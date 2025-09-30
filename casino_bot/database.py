from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class User:
    telegram_id: int
    username: str | None
    balance: int
    last_daily_timestamp: int | None


class CasinoDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_daily_timestamp INTEGER
                )
                """
            )

    def get_user(self, telegram_id: int) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT telegram_id, username, balance, last_daily_timestamp FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            if row is None:
                return None
            return User(
                telegram_id=row["telegram_id"],
                username=row["username"],
                balance=row["balance"],
                last_daily_timestamp=row["last_daily_timestamp"],
            )

    def create_user(self, telegram_id: int, username: str | None, starting_balance: int) -> User:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username, balance) VALUES (?, ?, ?)",
                (telegram_id, username, starting_balance),
            )
        return User(
            telegram_id=telegram_id,
            username=username,
            balance=starting_balance,
            last_daily_timestamp=None,
        )

    def get_user_by_username(self, username: str) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT telegram_id, username, balance, last_daily_timestamp
                FROM users WHERE username = ?
                """,
                (username,),
            ).fetchone()
            if row is None:
                return None
            return User(
                telegram_id=row["telegram_id"],
                username=row["username"],
                balance=row["balance"],
                last_daily_timestamp=row["last_daily_timestamp"],
            )

    def update_username(self, telegram_id: int, username: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ?",
                (username, telegram_id),
            )

    def set_balance(self, telegram_id: int, new_balance: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET balance = ? WHERE telegram_id = ?",
                (new_balance, telegram_id),
            )

    def adjust_balance(self, telegram_id: int, delta: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("User not found")
            new_balance = row["balance"] + delta
            if new_balance < 0:
                raise ValueError("Insufficient funds")
            conn.execute(
                "UPDATE users SET balance = ? WHERE telegram_id = ?",
                (new_balance, telegram_id),
            )
            return new_balance

    def set_daily_timestamp(self, telegram_id: int, timestamp: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET last_daily_timestamp = ? WHERE telegram_id = ?",
                (timestamp, telegram_id),
            )

    def transfer(self, sender_id: int, recipient_id: int, amount: int) -> tuple[int, int]:
        if amount <= 0:
            raise ValueError("Transfer amount must be positive")
        with self._connect() as conn:
            sender_row = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?",
                (sender_id,),
            ).fetchone()
            recipient_row = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?",
                (recipient_id,),
            ).fetchone()
            if sender_row is None or recipient_row is None:
                raise ValueError("Both users must be registered")
            if sender_row["balance"] < amount:
                raise ValueError("Insufficient funds")
            new_sender_balance = sender_row["balance"] - amount
            new_recipient_balance = recipient_row["balance"] + amount
            conn.execute(
                "UPDATE users SET balance = ? WHERE telegram_id = ?",
                (new_sender_balance, sender_id),
            )
            conn.execute(
                "UPDATE users SET balance = ? WHERE telegram_id = ?",
                (new_recipient_balance, recipient_id),
            )
            return new_sender_balance, new_recipient_balance

    def top_users(self, limit: int) -> list[User]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT telegram_id, username, balance, last_daily_timestamp
                FROM users
                ORDER BY balance DESC, telegram_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            User(
                telegram_id=row["telegram_id"],
                username=row["username"],
                balance=row["balance"],
                last_daily_timestamp=row["last_daily_timestamp"],
            )
            for row in rows
        ]
