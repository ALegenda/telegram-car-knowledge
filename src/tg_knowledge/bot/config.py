from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    telegram_token: str = field(repr=False)
    openai_key: str = field(repr=False)
    username: str = "tg_channel_parse_bot"
    model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    dimensions: int = 512
    allowed_chats: frozenset[int] = frozenset()

    @property
    def vector_model(self) -> str:
        return f"{self.embedding_model}:{self.dimensions}"

    @classmethod
    def from_env(cls, *, credentials: bool = True) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if credentials and (not token or not key):
            raise ValueError("Нужны TELEGRAM_BOT_TOKEN и OPENAI_API_KEY.")
        dimensions = int(os.getenv("BOT_EMBEDDING_DIMENSIONS", "512"))
        if not 64 <= dimensions <= 3072:
            raise ValueError("BOT_EMBEDDING_DIMENSIONS вне диапазона 64–3072.")
        return cls(
            root=Path(os.getenv("BOT_DATA_DIR", "data/bot")).resolve(),
            telegram_token=token,
            openai_key=key,
            username=os.getenv("BOT_USERNAME", "tg_channel_parse_bot").lstrip("@"),
            model=os.getenv("BOT_MODEL", "gpt-4.1-mini"),
            embedding_model=os.getenv("BOT_EMBEDDING_MODEL", "text-embedding-3-small"),
            dimensions=dimensions,
            allowed_chats=frozenset(
                int(value.strip())
                for value in os.getenv("BOT_ALLOWED_CHAT_IDS", "").split(",")
                if value.strip()
            ),
        )
