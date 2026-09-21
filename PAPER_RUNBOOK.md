# Paper Runbook — Apex Signals Now Instance #2

1. `cd /workspace/cruzbot_instance_2`
2. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env` — placeholders only until you inject secrets locally
4. `unset ENTRY_THRESHOLD TRADE_PROFILE WINNING_FORMULA STOP_LOSS_PROFILE`
5. Ensure a single `main.py` process for this instance
6. `python main.py` or smoke with `python main.py --once`
7. Telegram `/status` should read **`Apex Signals Now PAPER status`**
8. Ops: `/winning_formula on` then `/status`
9. Never commit `.env`. Never go live without operator + `/confirm_live`.
