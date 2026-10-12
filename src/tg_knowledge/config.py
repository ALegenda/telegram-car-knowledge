from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CHAT = "https://t.me/c/FangChengBaoClub/84505"
DEFAULT_DATA = "data/fangchengbao"


def load_config() -> None:
    load_dotenv(Path.cwd() / ".env")
    os.umask(0o077)


def data_dir(value: str | None = None) -> Path:
    return Path(value or os.getenv("TG_DATA_DIR", DEFAULT_DATA)).resolve()


def client_config() -> tuple[int, str, Path]:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id.isdigit() or int(api_id) <= 0 or not api_hash:
        raise ValueError("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.")
    session = Path(os.getenv("TELEGRAM_SESSION", "secrets/telegram")).resolve()
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return int(api_id), api_hash, session
