from __future__ import annotations

from typing import Any, Sequence

from .config import Settings
from .slots import FruitMachine, PirateMachine, SlotMachine, WildJackpotMachine


class MachineFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_all(self) -> dict[str, SlotMachine]:
        machines: dict[str, SlotMachine] = {}
        for cfg in self.settings.slot_machines:
            machine = self._create_machine_from_config(cfg)
            machines[machine.key] = machine
        if not machines:
            default = FruitMachine(self.settings.slot_reel, self.settings.special_payouts)
            machines[default.key] = default
        return machines

    def _create_machine_from_config(self, cfg: dict[str, Any]) -> SlotMachine:
        machine_type = (cfg.get("type") or cfg.get("key") or "").lower()
        key = (cfg.get("key") or machine_type).lower()
        if not key:
            raise ValueError("Slot machine config must include 'key'")
        if machine_type in {"pharaoh", "wild", "jackpot"}:
            machine = self._create_wild_machine(cfg)
        elif machine_type == "pirate":
            machine = PirateMachine()
            if "title" in cfg:
                machine.title = cfg["title"]
            if "description" in cfg:
                machine.description = cfg["description"]
        else:
            reel = self._normalize_reel(cfg.get("reel"))
            payouts = self._normalize_payouts(cfg.get("special_payouts"))
            machine = FruitMachine(reel, payouts)
            machine.key = key
            if "title" in cfg:
                machine.title = cfg["title"]
            if "description" in cfg:
                machine.description = cfg["description"]
            return machine
        machine.key = key
        if "title" in cfg:
            machine.title = cfg["title"]
        if "description" in cfg:
            machine.description = cfg["description"]
        return machine

    def _create_wild_machine(self, cfg: dict[str, Any]) -> SlotMachine:
        jackpot_percent = self._as_float(cfg.get("jackpot_percent"), 0.05)
        reel = self._normalize_reel(cfg.get("reel"))
        wild_symbol = str(cfg.get("wild_symbol", "🗿"))
        triple = self._normalize_symbol_map(
            cfg.get("triple_payouts"),
            default={"🐍": 20, "🐞": 16, "👁️": 12, "🏺": 10},
        )
        double = self._normalize_symbol_map(
            cfg.get("double_payouts"),
            default={"🐍": 5, "🐞": 4, "👁️": 3, "🏺": 2},
        )
        jackpot_multiplier = int(cfg.get("jackpot_multiplier", 60))
        jackpot_message = cfg.get("jackpot_message")
        seed = int(cfg.get("jackpot_seed", cfg.get("start_jackpot", 5000)))
        return WildJackpotMachine(
            reel,
            wild_symbol=wild_symbol,
            jackpot_percent=jackpot_percent,
            triple_payouts=triple,
            double_payouts=double,
            jackpot_multiplier=jackpot_multiplier,
            jackpot_message=jackpot_message,
            title=cfg.get("title"),
            description=cfg.get("description"),
            jackpot_seed=seed,
        )

    def _normalize_reel(self, raw) -> Sequence[str]:
        if not raw:
            return tuple(self.settings.slot_reel)
        if isinstance(raw, (list, tuple)):
            return tuple(str(item) for item in raw if str(item))
        if isinstance(raw, str):
            parts = [part.strip() for part in raw.split(",") if part.strip()]
            if parts:
                return tuple(parts)
        return tuple(self.settings.slot_reel)

    def _normalize_payouts(self, raw) -> dict[tuple[str, str, str], int]:
        if not raw:
            return dict(self.settings.special_payouts)
        result: dict[tuple[str, str, str], int] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                triplet = self._normalize_triplet(key)
                if triplet is None:
                    continue
                try:
                    result[triplet] = int(value)
                except (TypeError, ValueError):
                    continue
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                triplet = self._normalize_triplet(item.get("symbols"))
                if triplet is None:
                    continue
                try:
                    multiplier = int(item.get("multiplier"))
                except (TypeError, ValueError):
                    continue
                result[triplet] = multiplier
        return result or dict(self.settings.special_payouts)

    def _normalize_triplet(self, raw) -> tuple[str, str, str] | None:
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            return tuple(str(item) for item in raw)
        if isinstance(raw, str):
            cleaned = raw.strip().strip("[]")
            if "," in cleaned:
                parts = [part.strip().strip('"').strip("'") for part in cleaned.split(",") if part.strip()]
                if len(parts) == 3:
                    return tuple(parts)
            if len(cleaned) >= 3:
                chars = list(cleaned)
                if len(chars) >= 3:
                    return tuple(chars[:3])
        return None

    def _as_float(self, raw, default: float) -> float:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _normalize_symbol_map(self, raw, default: dict[str, int]) -> dict[str, int]:
        if not raw:
            return dict(default)
        result: dict[str, int] = {}
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            items = []
            for item in raw:
                if isinstance(item, dict) and "symbol" in item and "multiplier" in item:
                    items.append((item["symbol"], item["multiplier"]))
        else:
            return dict(default)
        for symbol, value in items:
            try:
                result[str(symbol)] = int(value)
            except (TypeError, ValueError):
                continue
        return result or dict(default)


__all__ = ["MachineFactory"]
