# Seer Bot

Telegram watcher for the Gnosis Chain Seer registry (`0x5aAF9E23A11440F8C1Ad6D2E2e5109C7e52CC672`). The script polls confirmed blocks, decodes contract events, and posts curated HTML notifications to Telegram with links back to Seer and Curate.

## Features
- Monitors the Light Generalized TCR registry on Gnosis via JSON-RPC.
- Waits for configurable confirmations and resumes from the last processed block stored in `state.json`.
- Deduplicates multi-event transactions so each item submission triggers exactly one Telegram message.
- Auto-detects Telegram group → supergroup migrations and reuses the new chat id without manual intervention.
- Emits rich logging with checksummed addresses for easy cross-referencing in explorers.

## Requirements
- Python 3.10+
- A Telegram bot token issued by [@BotFather](https://core.telegram.org/bots#botfather)
- The numeric `chat_id` (negative for groups/supergroups, positive for direct chats)

## Setup
1. Clone the repository and open the project directory.
2. (Optional) create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy the sample environment file and populate it:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` with your bot token and chat id. Keep the chat id updated if Telegram migrates the conversation to a supergroup.

## Configuration
Environment variables are read via `python-dotenv`:

- `TELEGRAM_BOT_TOKEN` (required) – Bot token from BotFather.
- `TELEGRAM_CHAT_ID` (required) – Destination chat or channel id, e.g. `YOUR_TELEGRAM_CHAT_ID`.
- `GNOSIS_RPC_URL` – HTTPS endpoint for Gnosis RPC (defaults to `https://rpc.gnosischain.com`).
- `CONFIRMATIONS` – Minimum confirmations to wait before notifying (default `3`).
- `POLL_INTERVAL` – Sleep duration between polling rounds in seconds (default `15`).
- `BATCH_SIZE` – Block span per `eth_getLogs` request (default `200`).
- `START_BLOCK` – Optional starting block; otherwise the script resumes from `state.json`.
- `REGISTRY_ADDRESS` – Registry contract to monitor (default points to the Seer registry).
- `EXPLORER_TX_URL` – Base URL used for transaction links (default GnosisScan).

You can override any variable inline when launching, e.g.:
```bash
LOG_LEVEL=DEBUG POLL_INTERVAL=10 python3 bot.py
```

## Running
Start the watcher with:
```bash
python3 bot.py
```
Logs stream to stdout. The watcher creates/updates `state.json` with the last processed block so restarts avoid re-sending older events.

Using `just` (requires `just` installed):
```bash
just rebuild
just restart
```

Defaults: `IMAGE=seer-bot`, `CONTAINER=seer-bot`, `ENV_FILE=.env`.

Override defaults inline, e.g.: `ENV_FILE=/abs/path/to/.env IMAGE=my-image CONTAINER=my-container just rebuild`.

## Operational Notes
- Notifications are sent only for `NewItem` and `RequestSubmitted` events and include Seer + Curate deep links using the on-chain item ID.
- Transaction hashes are deduplicated in-memory per process; restarting clears the seen set.
- The script retries automatically if Telegram migrates the chat id during execution.
- All addresses in logs and messages are rendered in checksum (EIP-55) form for consistency with block explorers.

## Troubleshooting
- Ensure the bot has been started by a user or added to the destination group/channel; otherwise Telegram rejects outbound messages.
- If you rotate the chat id (new group or migration), update `.env` before restarting the watcher.
- For verbose diagnostics, run with `LOG_LEVEL=DEBUG`.
