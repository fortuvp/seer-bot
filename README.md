# Seer Bot

Telegram watcher for the Gnosis Chain Seer registry (`0x5aAF9E23A11440F8C1Ad6D2E2e5109C7e52CC672`). It polls confirmed blocks, decodes events, and pushes HTML alerts to Telegram with Seer/Curate deep links and the on-chain market name.

## Features
- Monitors the Light Generalized TCR registry on Gnosis via JSON-RPC and listens for `NewItem`, `RequestSubmitted`, and `Dispute`.
- For submissions, fetches the `_data` IPFS JSON, extracts the market contract address, calls `marketName()`, and builds Seer + Curate links.
- For disputes, maps the dispute to the original `itemID` (via tx/evidence group), optionally resolves the market address through a subgraph, fetches `marketName()`, and posts a “challenged” alert with Curate (and Seer when address is known).
- Waits for configurable confirmations and resumes from the last processed block stored in `state.json`.
- Deduplicates per transaction so each submission triggers one notification; handles Telegram chat migrations automatically.
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
- `IPFS_GATEWAY` – Gateway base URL used to fetch IPFS metadata (default `https://ipfs.io`).
- `IPFS_TIMEOUT` – Timeout in seconds for IPFS HTTP fetches (default `20`).
- `SUBGRAPH_URL` – GraphQL endpoint (e.g., Envio/Hyperindex) to resolve the market address by itemID when it isn’t available from IPFS

You can override any variable inline when launching, e.g.:
```bash
LOG_LEVEL=DEBUG POLL_INTERVAL=10 python3 bot.py
```

## Running
Run locally:
1. (Recommended) create and activate a venv:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure `.env` (copy from `.env.example`) with `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and any overrides (`SUBGRAPH_URL` is optional for dispute address resolution).
4. Start the watcher:
   ```bash
   python3 bot.py
   ```
Logs stream to stdout; `state.json` is updated with the last processed block so restarts don’t replay beyond the confirmation buffer.

## Operational Notes
- Notifications are sent for `NewItem`, `RequestSubmitted`, and `Dispute`. Submissions fetch the `_data` IPFS JSON, derive the market address for the Seer link, and call `marketName()`; Curate always uses the on-chain `itemID`.
- For disputes, the bot maps the dispute to its `itemID` (via tx/evidence group), optionally uses `SUBGRAPH_URL` to recover the market address if not known, then calls `marketName()`; if the address cannot be resolved, only the Curate link is sent.
- Transaction hashes are deduplicated in-memory per process; restarting clears the seen set.
- The script retries automatically if Telegram migrates the chat id during execution.
- All addresses in logs and messages are rendered in checksum (EIP-55) form for consistency with block explorers.

## Troubleshooting
- Ensure the bot has been started by a user or added to the destination group/channel; otherwise Telegram rejects outbound messages.
- If you rotate the chat id (new group or migration), update `.env` before restarting the watcher.
- For verbose diagnostics, run with `LOG_LEVEL=DEBUG`.
