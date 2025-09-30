from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


_NotProvided = object()


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jackpots (
                    machine_key TEXT PRIMARY KEY,
                    amount INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_items (
                    user_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(user_id, item_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    title_id INTEGER,
                    balance_icon_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_effects (
                    user_id INTEGER NOT NULL,
                    effect_type TEXT NOT NULL,
                    item_id INTEGER,
                    expires_at INTEGER,
                    value REAL,
                    PRIMARY KEY (user_id, effect_type)
                )
                """
            )
            try:
                has_quantity = any(
                    row["name"] == "quantity"
                    for row in conn.execute("PRAGMA table_info(user_items)").fetchall()
                )
            except sqlite3.DatabaseError:
                has_quantity = True
            if not has_quantity:
                conn.execute("ALTER TABLE user_items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")

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

    def adjust_balance(
        self,
        telegram_id: int,
        delta: int,
        *,
        allow_overdraft: bool = False,
        overdraft_limit: int = 0,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("User not found")
            current_balance = row["balance"]
            new_balance = current_balance + delta
            min_balance = -overdraft_limit if allow_overdraft else 0
            if new_balance < min_balance:
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

    def add_to_jackpot(self, machine_key: str, amount: int) -> int:
        if amount <= 0:
            return self.get_jackpot(machine_key)
        with self._connect() as conn:
            current = conn.execute(
                "SELECT amount FROM jackpots WHERE machine_key = ?",
                (machine_key,),
            ).fetchone()
            if current is None:
                seed = max(amount, self._jackpot_seed(machine_key))
                conn.execute(
                    "INSERT INTO jackpots (machine_key, amount) VALUES (?, ?)",
                    (machine_key, seed),
                )
                return seed
            new_amount = current["amount"] + amount
            conn.execute(
                "UPDATE jackpots SET amount = ? WHERE machine_key = ?",
                (new_amount, machine_key),
            )
            return new_amount

    def reset_jackpot(self, machine_key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT amount FROM jackpots WHERE machine_key = ?",
                (machine_key,),
            ).fetchone()
            amount = row["amount"] if row else 0
            seed = self._jackpot_seed(machine_key)
            conn.execute(
                "REPLACE INTO jackpots (machine_key, amount) VALUES (?, ?)",
                (machine_key, seed),
            )
            return amount

    def get_jackpot(self, machine_key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT amount FROM jackpots WHERE machine_key = ?",
                (machine_key,),
            ).fetchone()
            if row is not None:
                return row["amount"]
        seed = self._jackpot_seed(machine_key)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jackpots (machine_key, amount) VALUES (?, ?)",
                (machine_key, seed),
            )
        return seed

    def _jackpot_seed(self, machine_key: str) -> int:
        seeds = self._jackpot_seeds
        return max(0, int(seeds.get(machine_key, 0)))

    @property
    def _jackpot_seeds(self) -> dict[str, int]:
        try:
            from .config import Settings

            seeds = {}
            settings = Settings.from_env()
            for cfg in settings.slot_machines:
                key = (cfg.get("key") or cfg.get("type") or "").lower()
                if not key:
                    continue
                if cfg.get("type") in {"pharaoh", "wild", "jackpot"}:
                    seeds[key] = int(cfg.get("jackpot_seed", cfg.get("start_jackpot", 0)))
            return seeds
        except Exception:
            return {}

    def add_item_to_inventory(self, telegram_id: int, item_id: int, *, stackable: bool = False) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
                (telegram_id, item_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO user_items (user_id, item_id, quantity) VALUES (?, ?, 1)",
                    (telegram_id, item_id),
                )
            elif stackable:
                conn.execute(
                    "UPDATE user_items SET quantity = quantity + 1 WHERE user_id = ? AND item_id = ?",
                    (telegram_id, item_id),
                )

    def consume_item(self, telegram_id: int, item_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
                (telegram_id, item_id),
            ).fetchone()
            if row is None or row["quantity"] <= 0:
                return False
            if row["quantity"] == 1:
                conn.execute(
                    "DELETE FROM user_items WHERE user_id = ? AND item_id = ?",
                    (telegram_id, item_id),
                )
            else:
                conn.execute(
                    "UPDATE user_items SET quantity = quantity - 1 WHERE user_id = ? AND item_id = ?",
                    (telegram_id, item_id),
                )
            return True

    def get_inventory(self, telegram_id: int) -> list[tuple[int, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, quantity FROM user_items WHERE user_id = ? ORDER BY item_id",
                (telegram_id,),
            ).fetchall()
        return [(row["item_id"], row["quantity"]) for row in rows]

    def has_item(self, telegram_id: int, item_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
                (telegram_id, item_id),
            ).fetchone()
            return bool(row and row["quantity"] > 0)

    def get_item_quantity(self, telegram_id: int, item_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
                (telegram_id, item_id),
            ).fetchone()
        if row is None:
            return 0
        return int(row["quantity"])

    def set_active_title(self, telegram_id: int, item_id: int | None) -> None:
        self._update_profile(telegram_id, title_id=item_id)

    def set_active_icon(self, telegram_id: int, item_id: int | None) -> None:
        self._update_profile(telegram_id, balance_icon_id=item_id)

    def get_profile(self, telegram_id: int) -> dict[str, int | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title_id, balance_icon_id FROM user_profiles WHERE user_id = ?",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return {"title_id": None, "balance_icon_id": None}
        return {"title_id": row["title_id"], "balance_icon_id": row["balance_icon_id"]}

    def set_effect(
        self,
        telegram_id: int,
        effect_type: str,
        *,
        item_id: int | None,
        expires_at: int,
        value: float | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "REPLACE INTO user_effects (user_id, effect_type, item_id, expires_at, value) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, effect_type, item_id, expires_at, value),
            )

    def clear_effect(self, telegram_id: int, effect_type: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM user_effects WHERE user_id = ? AND effect_type = ?",
                (telegram_id, effect_type),
            )

    def get_effect(self, telegram_id: int, effect_type: str) -> dict[str, float | int | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id, expires_at, value FROM user_effects WHERE user_id = ? AND effect_type = ?",
                (telegram_id, effect_type),
            ).fetchone()
        if row is None:
            return None
        return {"item_id": row["item_id"], "expires_at": row["expires_at"], "value": row["value"]}

    def _update_profile(self, telegram_id: int, *, title_id=_NotProvided, balance_icon_id=_NotProvided) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT title_id, balance_icon_id FROM user_profiles WHERE user_id = ?",
                (telegram_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO user_profiles (user_id, title_id, balance_icon_id) VALUES (?, ?, ?)",
                    (
                        telegram_id,
                        None if title_id is _NotProvided else title_id,
                        None if balance_icon_id is _NotProvided else balance_icon_id,
                    ),
                )
            else:
                new_title = existing["title_id"] if title_id is _NotProvided else title_id
                new_icon = existing["balance_icon_id"] if balance_icon_id is _NotProvided else balance_icon_id
                conn.execute(
                    "UPDATE user_profiles SET title_id = ?, balance_icon_id = ? WHERE user_id = ?",
                    (new_title, new_icon, telegram_id),
                )
