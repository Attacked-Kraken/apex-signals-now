#!/usr/bin/env bash
# Start Apex paper bot with .env Telegram credentials winning over shell placeholders.
#
# TG listener auto-heal prefers in-process restart (see TradingApp._maybe_heal_telegram_listener).
# Full process restart via this script only if the listener cannot recover (lock stuck,
# process hung). Prefer: bash scripts/start_paper.sh  (kills prior PID if you stop it first).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data logs
# Prefer .env over inherited placeholders; keep New Guy / BACKUP tokens separate.
exec env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID \
  "$ROOT/.venv/bin/python" -u "$ROOT/main.py"
