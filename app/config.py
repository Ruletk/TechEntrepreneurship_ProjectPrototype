import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_url: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var is required")

    db_url = os.getenv("DB_URL", "sqlite:///./bot.db")
    return Config(bot_token=token, db_url=db_url)
