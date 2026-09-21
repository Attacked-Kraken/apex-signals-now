import fcntl
import os
from pathlib import Path

from trading_bot.telegram_commands import TelegramCommandListener


def test_lock_path_uses_bot_id():
    tg = TelegramCommandListener("123456:ABC-DEF", "99", {})
    assert tg.bot_id == "123456"
    assert tg.lock_path == "/tmp/cruzbot_tg_123456.lock"


def test_flock_exclusive(tmp_path, monkeypatch):
    # Use a temp lock path by subclassing bot_id behavior
    lock = tmp_path / "cruzbot_tg_test.lock"
    fd1 = open(lock, "w")
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fd2 = open(lock, "w")
    blocked = False
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        blocked = True
    assert blocked is True
    fcntl.flock(fd1, fcntl.LOCK_UN)
    fd1.close()
    fd2.close()
