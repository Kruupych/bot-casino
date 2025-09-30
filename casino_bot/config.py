from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Sequence


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _sequence_env(name: str, default: Sequence[str]) -> Sequence[str]:
    raw = os.getenv(name)
    if not raw:
        return tuple(default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(parsed) if parsed else tuple(default)


def _payouts_env(name: str, default: dict[tuple[str, str, str], int]) -> dict[tuple[str, str, str], int]:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return default
    payouts: dict[tuple[str, str, str], int] = {}
    for key, multiplier in data.items():
        if not isinstance(key, (list, tuple)) or len(key) != 3:
            continue
        try:
            sym_tuple = tuple(str(s) for s in key)
            payouts[sym_tuple] = int(multiplier)
        except (TypeError, ValueError):
            continue
    return payouts or default


def _machines_env(name: str, default: Sequence[dict[str, Any]]) -> Sequence[dict[str, Any]]:
    raw = os.getenv(name)
    if not raw:
        return tuple(default)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return tuple(default)
    if isinstance(data, list):
        filtered: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict) and "key" in item:
                filtered.append(item)
        return tuple(filtered) if filtered else tuple(default)
    return tuple(default)


@dataclass(frozen=True)
class Settings:
    starting_balance: int = 1000
    daily_bonus: int = 200
    daily_cooldown_seconds: int = 24 * 60 * 60
    leaderboard_limit: int = 5
    slot_reel: Sequence[str] = field(default_factory=lambda: ("🍒", "🍋", "🍊", "🍇", "💎", "🔔", "🍀"))
    special_payouts: dict[tuple[str, str, str], int] = field(
        default_factory=lambda: {
            ("💎", "💎", "💎"): 50,
            ("🍀", "🍀", "🍀"): 20,
            ("🔔", "🔔", "🔔"): 10,
        }
    )
    slot_machines: Sequence[dict[str, Any]] = field(
        default_factory=lambda: (
            {
                "key": "fruit",
                "title": "Фруктовый Коктейль",
                "description": "Классический автомат с простыми правилами и быстрыми выигрышами.",
                "reel": ("🍒", "🍋", "🍊", "🍇", "💎", "🔔", "🍀"),
                "special_payouts": {
                    ("💎", "💎", "💎"): 50,
                    ("🍀", "🍀", "🍀"): 20,
                    ("🔔", "🔔", "🔔"): 10,
                },
            },
            {
                "key": "pharaoh",
                "title": "Золото Фараона",
                "description": "Автомат с диким символом Фараона. Wild заменяет любые символы и удваивает выигрыш.",
                "type": "pharaoh",
                "reel": ("🐍", "🐞", "👁️", "🏺", "𓇶", "🗿"),
                "wild_symbol": "🗿",
                "jackpot_percent": 0.01,
                "triple_payouts": {"🐍": 20, "🐞": 16, "👁️": 12, "🏺": 10, "𓇶": 6},
                "double_payouts": {"🐍": 2, "🐞": 2, "👁️": 2, "🏺": 2, "𓇶": 1},
                "jackpot_multiplier": 60,
            },
            {
                "key": "pirate",
                "title": "Сокровища Пирата",
                "description": "Собери 3 карты 🗺️ и получи 10 бесплатных вращений!",
                "type": "pirate",
            },
            {
                "key": "space",
                "title": "Космический Куш",
                "description": "Собери 3 черные дыры 🌌, чтобы сорвать межгалактический джекпот!",
                "type": "pharaoh",
                "reel": ("🪐", "🚀", "👽", "☄️", "✨", "🌌"),
                "wild_symbol": "🌌",
                "jackpot_percent": 0.015,
                "triple_payouts": {"🪐": 25, "🚀": 18, "👽": 14, "☄️": 10, "✨": 8},
                "double_payouts": {"🪐": 2, "🚀": 2, "👽": 2, "☄️": 2, "✨": 1},
                "jackpot_multiplier": 75,
            },
        )
    )

    @classmethod
    def from_env(cls) -> Settings:
        starting_balance = _int_env("CASINO_STARTING_BALANCE", cls.starting_balance)
        daily_bonus = _int_env("CASINO_DAILY_BONUS", cls.daily_bonus)
        daily_cooldown_seconds = _int_env("CASINO_DAILY_COOLDOWN", cls.daily_cooldown_seconds)
        leaderboard_limit = _int_env("CASINO_LEADERBOARD_LIMIT", cls.leaderboard_limit)
        slot_reel = _sequence_env("CASINO_SLOT_REEL", cls().slot_reel)
        special_payouts = _payouts_env("CASINO_SPECIAL_PAYOUTS", cls().special_payouts)
        slot_machines = _machines_env("CASINO_SLOT_MACHINES", cls().slot_machines)
        return cls(
            starting_balance=starting_balance,
            daily_bonus=daily_bonus,
            daily_cooldown_seconds=daily_cooldown_seconds,
            leaderboard_limit=leaderboard_limit,
            slot_reel=slot_reel,
            special_payouts=special_payouts,
            slot_machines=slot_machines,
        )


__all__ = ["Settings"]
