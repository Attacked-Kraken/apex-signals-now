"""Backup communications: New Guy Telegram mirror, Twilio SMS stub, email stub.

Tokens stay separate — backup TG uses New Guy bot token (or BACKUP_*), never
merged into primary TELEGRAM_BOT_TOKEN. SMS/email no-op until fully configured.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

NEWGUY_ENV_PATH = Path("/workspace/The-New-Guy-Cruz/.env")
CRUZBOT_ENV_PATH = Path("/workspace/cruzbot_instance_2/.env")
DEFAULT_BACKUP_CHAT_ID = "6949546419"
CRITICAL_TG_MAX = 500
CRITICAL_SMS_MAX = 320

# BUY/SELL/WIN/LOSS/CB trip/resume/listener heal — short critical alerts only
_CRITICAL_RE = re.compile(
    r"(?i)(\bBUY\b|\bSELL\b|\bWIN\b|\bLOSS\b|"
    r"circuit.?breaker|\bCB\b|cb[\s_-]?pause|cb[\s_-]?trip|cb[\s_-]?auto|"
    r"auto-?resume|listener|auto-?heal|telegram.*(dead|heal|restart)|"
    r"placeholder.?token|tg\s+dead|backup\s+ping)"
)


def is_critical_alert(text: str) -> bool:
    if not text or not str(text).strip():
        return False
    return bool(_CRITICAL_RE.search(str(text)))


def _read_dotenv_key(path: Path, key: str) -> str:
    """Read a single KEY=value from a .env file. Never logs the value."""
    try:
        if not path.is_file():
            return ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        logger.debug("dotenv read %s failed: %s", key, type(exc).__name__)
    return ""



def env_get(key: str) -> str:
    """Prefer process env, then cruzbot .env, then New Guy .env. Never logs values."""
    val = (os.environ.get(key) or "").strip()
    if val:
        return val
    for path in (CRUZBOT_ENV_PATH, NEWGUY_ENV_PATH):
        val = _read_dotenv_key(path, key)
        if val:
            return val
    return ""

def _is_placeholder_token(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return True
    u = t.upper()
    if u.startswith("YOUR_") or u in {"CHANGEME", "PLACEHOLDER", "TODO"}:
        return True
    if ":" not in t:
        return True
    return False


def resolve_backup_telegram() -> Tuple[str, str, str]:
    """Return (token, chat_id, source) for backup Telegram. Empty token if unset.

    Priority: BACKUP_TELEGRAM_* → NEWGUY_TELEGRAM_* → New Guy .env TELEGRAM_BOT_TOKEN
    + chat BACKUP/NEWGUY/primary TELEGRAM_CHAT_ID / default 6949546419.
    """
    token = (
        env_get("BACKUP_TELEGRAM_BOT_TOKEN")
        or env_get("NEWGUY_TELEGRAM_BOT_TOKEN")
    )
    source = "env"
    if not token or _is_placeholder_token(token):
        token = _read_dotenv_key(NEWGUY_ENV_PATH, "TELEGRAM_BOT_TOKEN")
        source = "newguy_env" if token else "none"
    # Prefer explicit backup chat; else cruzbot .env chat; else known Apex chat.
    # Avoid polluted shell TELEGRAM_CHAT_ID placeholders.
    chat = (
        (os.environ.get("BACKUP_TELEGRAM_CHAT_ID") or "").strip()
        or (os.environ.get("NEWGUY_TELEGRAM_CHAT_ID") or "").strip()
        or _read_dotenv_key(CRUZBOT_ENV_PATH, "BACKUP_TELEGRAM_CHAT_ID")
        or _read_dotenv_key(CRUZBOT_ENV_PATH, "NEWGUY_TELEGRAM_CHAT_ID")
        or _read_dotenv_key(CRUZBOT_ENV_PATH, "TELEGRAM_CHAT_ID")
        or DEFAULT_BACKUP_CHAT_ID
    )
    if _is_placeholder_token(token):
        return "", chat, "none"
    return token, chat, source


def normalize_e164(number: str) -> str:
    """Normalize US local 10-digit to +1… E.164. Does not log the number."""
    raw = (number or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and len(digits) >= 10:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if digits:
        return "+" + digits if not raw.startswith("+") else "+" + digits
    return ""


def twilio_config() -> Tuple[bool, dict]:
    """Return (ready, cfg). ready only when SID+token+from+to all present."""
    sid = env_get("TWILIO_ACCOUNT_SID")
    token = env_get("TWILIO_AUTH_TOKEN")
    frm = env_get("TWILIO_FROM_NUMBER")
    to = normalize_e164(env_get("SMS_TO_NUMBER"))
    cfg = {"sid": sid, "token": token, "from": frm, "to": to}
    ready = bool(sid and token and frm and to)
    return ready, cfg


def notify_sms(body: str, *, prefix: str = "Apex:") -> bool:
    """Send SMS if Twilio fully configured; otherwise silent no-op (debug log)."""
    ready, cfg = twilio_config()
    if not ready:
        logger.debug("SMS skipped — Twilio not fully configured")
        return False
    text = f"{prefix} {(body or '').strip()}".strip()
    if len(text) > CRITICAL_SMS_MAX:
        text = text[: CRITICAL_SMS_MAX - 1] + "…"
    try:
        from twilio.rest import Client as TwilioClient  # type: ignore
    except ImportError:
        logger.debug("SMS skipped — twilio package not installed")
        return False
    try:
        client = TwilioClient(cfg["sid"], cfg["token"])
        client.messages.create(body=text, from_=cfg["from"], to=cfg["to"])
        logger.info("SMS sent (len=%d prefix=%s)", len(text), prefix)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMS send failed: %s", type(exc).__name__)
        return False


def email_stub_configured() -> bool:
    to = env_get("BACKUP_EMAIL_TO")
    host = env_get("SMTP_HOST")
    return bool(to and host)


def notify_email_stub(subject: str, body: str) -> bool:
    """No-op until BACKUP_EMAIL_TO + SMTP_* configured."""
    if not email_stub_configured():
        logger.debug("email stub skipped — not configured")
        return False
    logger.info("email stub would send subject_len=%d body_len=%d (not implemented)", len(subject or ""), len(body or ""))
    return False


async def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """One-shot Telegram sendMessage via httpx. Never logs token."""
    if not token or not chat_id or not text:
        return False
    try:
        import httpx
    except ImportError:
        logger.warning("backup telegram: httpx missing")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": (text or "")[: CRITICAL_TG_MAX],
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning("backup telegram HTTP %s", r.status_code)
                return False
            data = r.json()
            if not data.get("ok"):
                logger.warning("backup telegram API not ok")
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("backup telegram failed: %s", type(exc).__name__)
        return False
