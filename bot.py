"""
Telegram bot watcher for registry contract events on Gnosis Chain.

This script polls the specified light TCR registry contract for new events,
formats them, and pushes the details to a Telegram chat via the Bot API.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set

import requests
from dotenv import load_dotenv
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract
from web3.types import EventData


load_dotenv()

DEFAULT_RPC_URL = "https://rpc.gnosischain.com"
DEFAULT_CONFIRMATIONS = 3
DEFAULT_POLL_INTERVAL = 15
DEFAULT_BATCH_SIZE = 200
MAX_MESSAGE_LENGTH = 3900
STATE_FILE = Path("state.json")
ABI_FILE = Path("abi.json")
REGISTRY_ADDRESS = "0x5aaf9e23a11440f8c1ad6d2e2e5109c7e52cc672"
EXPLORER_TX_URL = os.getenv("EXPLORER_TX_URL", "https://gnosisscan.io/tx/")


@dataclass
class Settings:
    rpc_url: str
    telegram_token: str
    telegram_chat_id: str
    confirmations: int
    poll_interval: int
    batch_size: int
    registry_address: str
    start_block: int | None


def load_settings() -> Settings:
    """Read configuration from environment variables with sensible defaults."""
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not telegram_token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required.")
    if not telegram_chat_id:
        raise ValueError("TELEGRAM_CHAT_ID environment variable is required.")

    rpc_url = os.getenv("GNOSIS_RPC_URL", DEFAULT_RPC_URL)
    confirmations = int(os.getenv("CONFIRMATIONS", DEFAULT_CONFIRMATIONS))
    poll_interval = int(os.getenv("POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
    batch_size = int(os.getenv("BATCH_SIZE", DEFAULT_BATCH_SIZE))
    registry_address = os.getenv("REGISTRY_ADDRESS", REGISTRY_ADDRESS)
    start_block_env = os.getenv("START_BLOCK")
    start_block = int(start_block_env) if start_block_env else None

    return Settings(
        rpc_url=rpc_url,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        confirmations=max(confirmations, 0),
        poll_interval=max(poll_interval, 1),
        batch_size=max(batch_size, 1),
        registry_address=registry_address,
        start_block=start_block,
    )


def build_contract(settings: Settings) -> Contract:
    if not ABI_FILE.exists():
        raise FileNotFoundError(f"Missing ABI file at {ABI_FILE}")

    with ABI_FILE.open("r", encoding="utf-8") as abi_file:
        abi = json.load(abi_file)

    w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        raise ConnectionError(f"Unable to connect to GNOSIS RPC at {settings.rpc_url}")

    checksum_address = Web3.to_checksum_address(settings.registry_address)

    contract = w3.eth.contract(address=checksum_address, abi=abi)
    contract.web3 = w3  # type: ignore[attr-defined]
    contract.w3 = w3  # type: ignore[attr-defined]
    return contract


def load_last_processed_block(default: int) -> int:
    if not STATE_FILE.exists():
        return default

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return int(data.get("last_processed_block", default))
    except (ValueError, json.JSONDecodeError):
        logging.warning("State file corrupted, defaulting to %s", default)
        return default


def save_last_processed_block(block_number: int) -> None:
    STATE_FILE.write_text(
        json.dumps({"last_processed_block": block_number}, indent=2),
        encoding="utf-8",
    )


def get_event_classes(contract: Contract) -> Sequence[Any]:
    event_names: List[str] = [
        entry["name"] for entry in contract.abi if entry.get("type") == "event"
    ]
    return [getattr(contract.events, name) for name in event_names]


def poll_for_events(
    contract: Contract,
    event_classes: Sequence[Any],
    from_block: int,
    to_block: int,
) -> List[EventData]:
    collected: List[EventData] = []
    for event_cls in event_classes:
        try:
            logs = event_cls.get_logs(fromBlock=from_block, toBlock=to_block)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to fetch %s events: %s", event_cls.event_name, exc)
            continue
        collected.extend(logs)

    collected.sort(
        key=lambda entry: (
            entry["blockNumber"],
            entry["transactionIndex"],
            entry["logIndex"],
        )
    )
    return collected


def try_checksum_address(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        prefixed = candidate if candidate.startswith(("0x", "0X")) else f"0x{candidate}"
        if Web3.is_address(prefixed):
            return Web3.to_checksum_address(prefixed)
        return None
    if isinstance(value, (bytes, bytearray, HexBytes)):
        if len(value) == 20:
            hex_value = Web3.to_hex(value)
            return Web3.to_checksum_address(hex_value)
    return None


def normalise_value(value: Any) -> str:
    checksum = try_checksum_address(value)
    if checksum is not None:
        return checksum
    if isinstance(value, HexBytes):
        return value.hex()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(normalise_value(item) for item in value) + "]"
    return str(value)


def format_event(event: EventData) -> str:
    args_lines = [
        f"• {key}: {normalise_value(value)}" for key, value in event["args"].items()
    ]
    explorer_url = f"{EXPLORER_TX_URL.rstrip('/')}/{event['transactionHash'].hex()}"
    body = "\n".join(args_lines) if args_lines else "• (no arguments)"
    return (
        f"Event: {event['event']}\n"
        f"Block: {event['blockNumber']}\n"
        f"Transaction: {explorer_url}\n"
        f"{body}"
    )


class TelegramSendError(RuntimeError):
    def __init__(
        self,
        message: str,
        migrate_to_chat_id: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.migrate_to_chat_id = migrate_to_chat_id
        self.retry_after = retry_after


def send_telegram_message(
    token: str,
    chat_id: str,
    message: str,
    *,
    parse_mode: str | None = None,
    disable_preview: bool | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if len(message) > MAX_MESSAGE_LENGTH:
        message = f"{message[:MAX_MESSAGE_LENGTH]}\n… (truncated)"
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_preview is not None:
        payload["disable_web_page_preview"] = disable_preview
    response = requests.post(
        url,
        json=payload,
        timeout=30,
    )
    migrate_to: int | None = None
    retry_after: int | None = None
    description: str | None = None
    try:
        response_json = response.json()
    except ValueError:
        response_json = None

    if isinstance(response_json, dict):
        if not response_json.get("ok", False):
            params = response_json.get("parameters", {})
            migrate_to = params.get("migrate_to_chat_id")
            retry_after = params.get("retry_after")
            description = response_json.get("description")
    elif not response.ok:
        description = response.text

    if description or not response.ok:
        raise TelegramSendError(
            f"Telegram API error ({response.status_code}): {description or response.text}",
            migrate_to_chat_id=migrate_to,
            retry_after=retry_after,
        )


def deliver_notification(
    token: str,
    chat_id: str,
    message: str,
    *,
    parse_mode: str | None = None,
    disable_preview: bool | None = None,
) -> str:
    current_chat_id = chat_id
    while True:
        try:
            send_telegram_message(
                token,
                current_chat_id,
                message,
                parse_mode=parse_mode,
                disable_preview=disable_preview,
            )
            return current_chat_id
        except TelegramSendError as err:
            if err.migrate_to_chat_id is not None:
                migrated_id = str(err.migrate_to_chat_id)
                if migrated_id != current_chat_id:
                    logging.warning(
                        "Chat migrated to %s; retrying delivery.", migrated_id
                    )
                    current_chat_id = migrated_id
                    continue
            raise


def determine_initial_block(contract: Contract, settings: Settings) -> int:
    latest = contract.web3.eth.block_number
    confirmed = max(latest - settings.confirmations, 0)
    if settings.start_block is not None:
        start = max(settings.start_block, 0)
        logging.info("Starting from configured block %s", start)
        return max(start - 1, -1)
    state_last = load_last_processed_block(confirmed)
    logging.info("Resuming from block %s", state_last)
    return state_last


def split_batches(start: int, end: int, size: int) -> Iterable[tuple[int, int]]:
    cursor = start
    while cursor <= end:
        batch_end = min(cursor + size - 1, end)
        yield cursor, batch_end
        cursor = batch_end + 1


def ensure_args_dict(args: Any) -> Dict[str, Any]:
    if isinstance(args, dict):
        return args
    if hasattr(args, "items"):
        return dict(args.items())
    return {}


def extract_item_id(args: Dict[str, Any]) -> str | None:
    raw = args.get("_itemID") or args.get("itemID")
    if raw is None:
        return None
    if isinstance(raw, HexBytes):
        return Web3.to_hex(raw).lower()
    if isinstance(raw, bytes):
        return Web3.to_hex(raw).lower()
    if isinstance(raw, int):
        return hex(raw).lower()
    if isinstance(raw, str):
        raw_str = raw.strip()
        if raw_str.startswith("0x"):
            return raw_str.lower()
        try:
            return hex(int(raw_str, 0)).lower()
        except ValueError:
            # Fallback: treat as hex without prefix if characters are hex-like
            try:
                return "0x" + bytes.fromhex(raw_str).hex()
            except ValueError:
                return raw_str
    return str(raw)


def build_notification_message(
    event_name: str | None, args: Any, contract_address: str
) -> str | None:
    if event_name not in {"NewItem", "RequestSubmitted"}:
        return None
    args_dict = ensure_args_dict(args)
    item_id = extract_item_id(args_dict)
    if not item_id:
        logging.warning("Skipping notification; could not extract item ID.")
        return None
    checksum_contract = Web3.to_checksum_address(contract_address)
    seer_url = f"https://app.seer.pm/markets/100/{item_id}"
    curate_url = f"https://curate.kleros.io/tcr/100/{checksum_contract}/{item_id}"
    return (
        "A new market has been submitted for verification.\n"
        f'Seer: <a href="{seer_url}">check here</a>\n'
        f'Curate: <a href="{curate_url}">check here</a>'
    )


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = load_settings()
    contract = build_contract(settings)
    event_classes = get_event_classes(contract)

    current_chat_id = settings.telegram_chat_id
    last_processed = determine_initial_block(contract, settings)

    logging.info(
        "Watching %d events on contract %s",
        len(event_classes),
        normalise_value(contract.address),
    )

    seen_transactions: Set[str] = set()

    while True:
        latest_block = contract.web3.eth.block_number
        target_block = max(latest_block - settings.confirmations, 0)

        if last_processed >= target_block:
            time.sleep(settings.poll_interval)
            continue

        window_start = last_processed + 1
        for batch_start, batch_end in split_batches(
            window_start, target_block, settings.batch_size
        ):
            logging.info("Querying blocks %s-%s", batch_start, batch_end)
            events = poll_for_events(contract, event_classes, batch_start, batch_end)
            for event in events:
                try:
                    tx_hash = (
                        event["transactionHash"].hex()
                        if event.get("transactionHash")
                        else "<unknown>"
                    )
                    raw_emitter = event.get("address")
                    emitter = (
                        normalise_value(raw_emitter)
                        if raw_emitter is not None
                        else "<unknown>"
                    )
                    args_dict = ensure_args_dict(event.get("args", {}))
                    formatted_args = {
                        key: normalise_value(val) for key, val in args_dict.items()
                    }
                    logging.info(
                        "Detected event %s | block=%s | tx=%s | address=%s | args=%s",
                        event.get("event"),
                        event.get("blockNumber"),
                        tx_hash,
                        emitter,
                        formatted_args,
                    )

                    if event.get("event") not in {"NewItem", "RequestSubmitted"}:
                        continue

                    if tx_hash != "<unknown>" and tx_hash in seen_transactions:
                        logging.info(
                            "Skipping duplicate notification for tx=%s", tx_hash
                        )
                        continue

                    message = build_notification_message(
                        event.get("event"),
                        args_dict,
                        contract.address,
                    )
                    if message:
                        delivered_chat_id = deliver_notification(
                            settings.telegram_token,
                            current_chat_id,
                            message,
                            parse_mode="HTML",
                            disable_preview=False,
                        )
                        current_chat_id = delivered_chat_id
                        settings.telegram_chat_id = delivered_chat_id
                        if tx_hash != "<unknown>":
                            seen_transactions.add(tx_hash)
                        logging.info(
                            "Sent notification for %s (block %s) | tx=%s",
                            event["event"],
                            event["blockNumber"],
                            tx_hash,
                        )
                except Exception as exc:  # noqa: BLE001
                    logging.exception("Failed to send notification: %s", exc)
            last_processed = batch_end
            save_last_processed_block(last_processed)

        time.sleep(settings.poll_interval)



if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logging.info("Shutting down watcher.")
