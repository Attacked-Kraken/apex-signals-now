#!/usr/bin/env python3
"""Build a sanitized zip of cruzbot_instance_2 (no secrets, no venv, no caches).

Usage:
  python scripts/package_codebase.py
  python scripts/package_codebase.py --out /tmp/cruzbot_i2_sanitized.zip

Excludes: .env*, .venv, __pycache__, .pytest_cache, *.pyc, private keys,
credential-looking JSON, large DBs/logs by default (keeps .env.example + empty data/).
"""
from __future__ import annotations

import argparse
import fnmatch
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIR_NAMES = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
}

EXCLUDE_FILE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*credentials*",
    "*secrets*",
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.dylib",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "paper_book*.json",
    "active_params*.json",
    "trade_memory.json",
]

# Always keep these even if they match a cautious pattern
KEEP_ALWAYS = {
    ".env.example",
    "requirements.txt",
    "README.md",
    "PAPER_RUNBOOK.md",
    "TELEGRAM_COMMANDS.md",
    "MASTER_SYSTEM_ARCHIVE.md",
}


def _excluded(rel: Path) -> bool:
    name = rel.name
    # Only honor KEEP_ALWAYS for top-level (or exact) paths — not nested cache READMEs.
    if str(rel) in KEEP_ALWAYS or (len(rel.parts) == 1 and name in KEEP_ALWAYS):
        return False
    for part in rel.parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    for pat in EXCLUDE_FILE_GLOBS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(str(rel).replace("\\", "/"), pat):
            # keep .env.example
            if name == ".env.example":
                return False
            return True
    # redact anything that looks like a real token file
    lower = name.lower()
    if lower.endswith(".env") and name != ".env.example":
        return True
    return False


def build_zip(out: Path) -> tuple[Path, int]:
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if _excluded(rel):
                continue
            zf.write(path, arcname=str(Path("cruzbot_instance_2") / rel))
            count += 1
        # Ensure empty data/ placeholder exists in zip
        zf.writestr("cruzbot_instance_2/data/.gitkeep", "")
    return out, count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / f"cruzbot_instance_2_sanitized_{ts}.zip",
    )
    args = ap.parse_args()
    # Don't write the zip into a path that would then exclude itself mid-build oddly;
    # data/*.zip is fine (not in exclude globs for zip).
    out, n = build_zip(args.out)
    print(f"Wrote {out} ({n} files, secrets/venv/caches excluded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
