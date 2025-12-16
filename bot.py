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
import html
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
IPFS_GATEWAY = os.getenv("IPFS_GATEWAY", "https://ipfs.io")
IPFS_TIMEOUT = int(os.getenv("IPFS_TIMEOUT", "20"))
SUBGRAPH_URL = os.getenv("SUBGRAPH_URL")


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


@dataclass
class MarketDetails:
    address: str | None
    name: str | None


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
    event_name: str | None,
    args: Any,
    contract_address: str,
    *,
    market_address_override: str | None = None,
    market_name: str | None = None,
) -> str | None:
    if event_name not in {"NewItem", "RequestSubmitted"}:
        return None
    args_dict = ensure_args_dict(args)
    item_id = extract_item_id(args_dict)
    if not item_id:
        logging.warning("Skipping notification; could not extract item ID.")
        return None
    target_id = market_address_override or item_id
    display_name = market_name or target_id
    checksum_contract = Web3.to_checksum_address(contract_address)
    seer_url = f"https://app.seer.pm/markets/100/{target_id}"
    curate_url = f"https://curate.kleros.io/tcr/100/{checksum_contract}/{item_id}"
    return (
        "<b>🔵🔵 NEW VERIFICATION REQUEST 🔵🔵</b>\n\n"
        "A new market has been submitted for verification.\n\n"
        f"Market: <b>{html.escape(display_name)}</b>\n\n"
        f'Seer: <a href="{seer_url}">Interact with the Market</a>\n'
        f'Curate: <a href="{curate_url}">Check Market Compliance</a>\n\n'
        "<i>💰Each market verification request comes with a $100 bounty. "
        "Spot a case of non-compliance, submit a challenge, and if you win, the bounty is yours.💰</i>"
    )


def build_dispute_message(
    item_id: str, contract_address: str, market_details: MarketDetails
) -> str:
    display_name = market_details.name or item_id
    checksum_contract = Web3.to_checksum_address(contract_address)
    curate_url = f"https://curate.kleros.io/tcr/100/{checksum_contract}/{item_id}"
    seer_line = ""
    if market_details.address:
        seer_url = f"https://app.seer.pm/markets/100/{market_details.address}"
        seer_line = f'Seer: <a href="{seer_url}">Interact with the Market</a>\n'
    return (
        "<b>❗️❗️ DISPUTED MARKET ❗️❗️</b>\n\n"
        "A market has been challenged.\n\n"
        f"Market: <b>{html.escape(display_name)}</b>\n\n"
        f"{seer_line}"
        f'Curate: <a href="{curate_url}">Follow the Dispute</a>'
    )


def build_ipfs_url(ipfs_path: str, gateway: str = IPFS_GATEWAY) -> str | None:
    path = ipfs_path.strip()
    if not path:
        return None
    if path.startswith("ipfs://"):
        path = path[len("ipfs://") :]
    if path.startswith("/ipfs/"):
        path = path[len("/ipfs/") :]
    if path.startswith("/"):
        path = path[1:]
    if not path:
        return None
    return f"{gateway.rstrip('/')}/ipfs/{path}"


def fetch_ipfs_json(ipfs_path: str) -> Dict[str, Any] | None:
    url = build_ipfs_url(ipfs_path)
    if not url:
        logging.warning("IPFS path %s could not be normalized", ipfs_path)
        return None
    try:
        response = requests.get(url, timeout=IPFS_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed to fetch IPFS content at %s: %s", url, exc)
        return None
    if not response.ok:
        logging.warning(
            "IPFS gateway returned %s for %s: %s",
            response.status_code,
            url,
            response.text,
        )
        return None
    try:
        return response.json()
    except ValueError:
        logging.warning("IPFS content at %s is not valid JSON", url)
        return None


MARKET_NAME_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "marketName",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def fetch_market_details(ipfs_path: str, w3: Web3) -> MarketDetails:
    details = MarketDetails(address=None, name=None)
    payload = fetch_ipfs_json(ipfs_path)
    if payload is None:
        return details

    def find_market_address(data: Dict[str, Any]) -> str | None:
        candidates = []
        for key in ("address", "market", "Market"):
            if key in data:
                candidates.append(data[key])
        values = data.get("values") or {}
        if isinstance(values, dict):
            for key in ("address", "market", "Market"):
                if key in values:
                    candidates.append(values[key])
        for candidate in candidates:
            checksum = try_checksum_address(candidate)
            if checksum:
                return checksum
        return None

    checksum_address = find_market_address(payload)
    if checksum_address:
        details.address = checksum_address
    else:
        logging.warning("No valid market address found in IPFS payload for %s", ipfs_path)

    if details.address:
        try:
            contract = w3.eth.contract(address=details.address, abi=MARKET_NAME_ABI)
            details.name = contract.functions.marketName().call()
        except Exception as exc:  # noqa: BLE001
            logging.warning(
                "Could not fetch marketName from %s: %s",
                details.address,
                exc,
            )

    cache[ipfs_path] = details
    return details


def fetch_market_name(address: str, w3: Web3) -> str | None:
    try:
        contract = w3.eth.contract(address=address, abi=MARKET_NAME_ABI)
        return contract.functions.marketName().call()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not fetch marketName from %s: %s", address, exc)
        return None


def fetch_market_from_subgraph(
    item_id: str, registry_address: str
) -> MarketDetails:
    if not SUBGRAPH_URL:
        return MarketDetails(address=None, name=None)
    query = """
    query ($registry: String!, $item: String!) {
      LItem(
        where: {
          registryAddress: { _eq: $registry }
          itemID: { _eq: $item }
        }
        limit: 1
      ) {
        key0
      }
    }
    """
    payload = {
        "query": query,
        "variables": {
            "registry": registry_address.lower(),
            "item": item_id.lower(),
        },
    }
    try:
        response = requests.post(SUBGRAPH_URL, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Subgraph request failed: %s", exc)
        return MarketDetails(address=None, name=None)
    try:
        items = data.get("data", {}).get("LItem", [])
        if items:
            addr = items[0].get("key0")
            checksum = try_checksum_address(addr)
            if checksum:
                return MarketDetails(address=checksum, name=None)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Unexpected subgraph payload: %s", exc)
    return MarketDetails(address=None, name=None)


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
    evidence_item_map: Dict[str, str] = {}
    tx_item_map: Dict[str, str] = {}

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
                    ipfs_path = args_dict.get("_data") or args_dict.get("data")
                    market_details = (
                        fetch_market_details(str(ipfs_path), contract.web3)
                        if ipfs_path is not None
                        else MarketDetails(address=None, name=None)
                    )
                    item_id = extract_item_id(args_dict) if args_dict else None
                    if item_id and tx_hash != "<unknown>":
                        tx_item_map[tx_hash] = item_id
                    if item_id:
                        if not market_details.address and SUBGRAPH_URL:
                            subgraph_details = fetch_market_from_subgraph(
                                item_id, contract.address
                            )
                            if subgraph_details.address:
                                if not subgraph_details.name:
                                    subgraph_details.name = fetch_market_name(
                                        subgraph_details.address, contract.web3
                                    )
                                market_details = subgraph_details
                        evidence_group = args_dict.get("_evidenceGroupID")
                        if evidence_group is not None:
                            evidence_item_map[str(evidence_group)] = item_id
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

                    if event.get("event") not in {"NewItem", "RequestSubmitted", "Dispute"}:
                        continue

                    if event.get("event") != "Dispute" and tx_hash != "<unknown>" and tx_hash in seen_transactions:
                        logging.info(
                            "Skipping duplicate notification for tx=%s", tx_hash
                        )
                        continue

                    message: str | None
                    if event.get("event") == "Dispute":
                        evidence_group = args_dict.get("_evidenceGroupID")
                        linked_item = evidence_item_map.get(str(evidence_group)) or tx_item_map.get(tx_hash)
                        linked_details = MarketDetails(None, None)
                        if linked_item:
                            subgraph_details = fetch_market_from_subgraph(
                                linked_item, contract.address
                            )
                            if subgraph_details.address and not subgraph_details.name:
                                subgraph_details.name = fetch_market_name(
                                    subgraph_details.address, contract.web3
                                )
                            linked_details = subgraph_details
                            if linked_details.address and not linked_details.name:
                                linked_details.name = fetch_market_name(
                                    linked_details.address, contract.web3
                                )
                        if not linked_item:
                            logging.warning(
                                "Dispute without known item ID (evidenceGroupID=%s)",
                                evidence_group,
                            )
                            continue
                        message = build_dispute_message(
                            linked_item, contract.address, linked_details
                        )
                    else:
                        message = build_notification_message(
                            event.get("event"),
                            args_dict,
                            contract.address,
                            market_address_override=market_details.address,
                            market_name=market_details.name,
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
