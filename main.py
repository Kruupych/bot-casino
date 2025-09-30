from __future__ import annotations

import os

import logging

from casino_bot.bot import build_application
from casino_bot.config import Settings
from casino_bot.database import CasinoDatabase
from casino_bot.env import load_dotenv


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")

    db_path = os.environ.get("CASINO_DB_PATH", "casino.sqlite3")
    settings = Settings.from_env()
    logging.info("Launching Bot-Casino with DB at %s", db_path)
    app = build_application(token, db_path=db_path, settings=settings)
    logging.info("Bot-Casino started. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
