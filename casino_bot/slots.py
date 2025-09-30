from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence


@dataclass
class SpinResult:
    symbols: tuple[str, str, str]
    winnings: int
    message: str
    jackpot_win: int = 0
    free_spins: int = 0


class SlotMachine:
    key: str
    title: str
    description: str

    def __init__(self, reel: Sequence[str]) -> None:
        self._reel: tuple[str, ...] = tuple(reel)
        if len(self._reel) == 0:
            raise ValueError("Reel must contain at least one symbol")

    @property
    def reel(self) -> Sequence[str]:
        return self._reel

    def spin(
        self,
        bet: int,
        rng: random.Random | None = None,
        *,
        jackpot_balance: int = 0,
    ) -> SpinResult:
        if rng is None:
            rng = random
        symbols = tuple(rng.choice(self._reel) for _ in range(3))  # type: ignore[arg-type]
        winnings, body, jackpot_win, extras = self.evaluate(symbols, bet, jackpot_balance)
        header = f"[ {' | '.join(symbols)} ]"
        message = f"{header}\n{body}"
        free_spins = extras.get("free_spins", 0)
        return SpinResult(
            symbols=symbols,
            winnings=winnings,
            message=message,
            jackpot_win=jackpot_win,
            free_spins=free_spins,
        )

    def evaluate(
        self, symbols: tuple[str, str, str], bet: int, jackpot_balance: int
    ) -> tuple[int, str, int, dict[str, int]]:
        raise NotImplementedError

    def supports_jackpot(self) -> bool:
        return False

    def jackpot_contribution(self, bet: int) -> int:
        return 0


class FruitMachine(SlotMachine):
    key = "fruit"
    title = "Фруктовый Коктейль"
    description = "Классический автомат с простыми правилами и быстрыми выигрышами."

    def __init__(
        self,
        reel: Sequence[str],
        special_payouts: dict[tuple[str, str, str], int],
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(reel)
        self._special_payouts = special_payouts
        if title:
            self.title = title
        if description:
            self.description = description

    def evaluate(
        self, symbols: tuple[str, str, str], bet: int, jackpot_balance: int
    ) -> tuple[int, str, int, dict[str, int]]:
        payout = self._special_payouts.get(symbols)
        if payout:
            winnings = bet * payout
            if symbols == ("💎", "💎", "💎"):
                return winnings, "💥 ДЖЕКПОТ! 💥 Вы выиграли {0} фишек!".format(winnings), 0, {}
            if symbols == ("🍀", "🍀", "🍀"):
                return winnings, "Удача на вашей стороне! Три клевера приносят {0} фишек.".format(winnings), 0, {}
            if symbols == ("🔔", "🔔", "🔔"):
                return winnings, "🔔 Звон монет! 🔔 Вы выиграли {0} фишек.".format(winnings), 0, {}
            return winnings, "Вы сорвали крупный выигрыш: {0} фишек!".format(winnings), 0, {}

        if symbols[0] == symbols[1] == symbols[2]:
            winnings = bet * 5
            return winnings, "Три совпадения! Вы выиграли {0} фишек.".format(winnings), 0, {}

        if len({symbols[0], symbols[1], symbols[2]}) == 2:
            winnings = bet * 2
            return winnings, "Два совпадения! Вы выиграли {0} фишек.".format(winnings), 0, {}

        return 0, "Увы, в этот раз не повезло. Попробуйте еще раз!", 0, {}


class WildJackpotMachine(SlotMachine):
    key = "jackpot"
    title = "Дикий Джекпот"
    description = "Wild-символ заменяет остальные, умножая выигрыш и пополняя джекпот."

    def __init__(
        self,
        reel: Sequence[str] | None = None,
        *,
        wild_symbol: str = "🗿",
        jackpot_percent: float = 0.01,
        triple_payouts: dict[str, int] | None = None,
        double_payouts: dict[str, int] | None = None,
        jackpot_multiplier: int = 60,
        jackpot_message: str | None = None,
        title: str | None = None,
        description: str | None = None,
        jackpot_seed: int = 0,
    ) -> None:
        reel = tuple(reel or ("🐍", "🐞", "👁️", "🏺", wild_symbol))
        super().__init__(reel)
        self._wild = wild_symbol
        self._jackpot_percent = max(0.0, jackpot_percent)
        self._triple_payouts = triple_payouts or {
            "🐍": 20,
            "🐞": 16,
            "👁️": 12,
            "🏺": 10,
        }
        self._double_payouts = double_payouts or {
            "🐍": 5,
            "🐞": 4,
            "👁️": 3,
            "🏺": 2,
        }
        self._jackpot_multiplier = jackpot_multiplier
        self._jackpot_message = (
            jackpot_message
            or "👑 Главный приз! Вы забираете джекпот в {jackpot} фишек + базовый выигрыш {base} (итого {total})."
        )
        self.jackpot_seed = max(0, jackpot_seed)
        if title:
            self.title = title
        if description:
            self.description = description

    def evaluate(
        self, symbols: tuple[str, str, str], bet: int, jackpot_balance: int
    ) -> tuple[int, str, int, dict[str, int]]:
        wild_count = symbols.count(self._wild)
        if wild_count == 3:
            base = bet * self._jackpot_multiplier
            total = base + jackpot_balance
            return (
                total,
                self._jackpot_message.format(jackpot=jackpot_balance, base=base, total=total),
                jackpot_balance,
                {},
            )

        non_wild = [s for s in symbols if s != self._wild]
        if wild_count:
            if not non_wild:
                base = bet * self._jackpot_multiplier
                total = base + jackpot_balance
                return (
                    total,
                    self._jackpot_message.format(jackpot=jackpot_balance, base=base, total=total),
                    jackpot_balance,
                    {},
                )

            best_symbol = self._choose_best_symbol(non_wild)
            matches = non_wild.count(best_symbol) + wild_count
            multiplier = 0
            if matches >= 3:
                multiplier = self._triple_payouts.get(best_symbol, 0)
            elif matches == 2:
                multiplier = self._double_payouts.get(best_symbol, 0)

            if multiplier:
                winnings = bet * multiplier
                if matches >= 3:
                    return winnings, (
                        f"{self._wild} поддержал комбинацию! Три {best_symbol} приносят {winnings} фишек."
                    ), 0, {}
                return winnings, (
                    f"{self._wild} дополнил ваш выигрыш! Пара {best_symbol} приносит {winnings} фишек."
                ), 0, {}

        if len(set(symbols)) == 1 and symbols[0] != self._wild:
            symbol = symbols[0]
            multiplier = self._triple_payouts.get(symbol, 0)
            if multiplier:
                winnings = bet * multiplier
                return winnings, "Три {0}! Вы выиграли {1} фишек.".format(symbol, winnings), 0, {}

        counts = {s: symbols.count(s) for s in set(symbols) if s != self._wild}
        for symbol, count in counts.items():
            if count == 2:
                multiplier = self._double_payouts.get(symbol, 0)
                if multiplier:
                    winnings = bet * multiplier
                    return winnings, "Пара {0} приносит {1} фишек.".format(symbol, winnings), 0, {}

        return 0, "Пески пусты. Попробуйте ещё раз!", 0, {}

    def _choose_best_symbol(self, symbols: list[str]) -> str:
        best = symbols[0]
        best_score = self._score_symbol(best)
        for symbol in symbols[1:]:
            score = self._score_symbol(symbol)
            if score > best_score:
                best = symbol
                best_score = score
        return best

    def _score_symbol(self, symbol: str) -> int:
        return self._triple_payouts.get(symbol, 0) * 10 + self._double_payouts.get(symbol, 0)

    def supports_jackpot(self) -> bool:
        return True

    def jackpot_contribution(self, bet: int) -> int:
        contribution = int(bet * self._jackpot_percent)
        if contribution <= 0 and bet > 0:
            contribution = 5
        return contribution


class PirateMachine(SlotMachine):
    key = "pirate"
    title = "Сокровища Пирата"
    description = "Собери 3 карты 🗺️ и получи бесплатные вращения!"

    _scatter = "🗺️"

    def __init__(self) -> None:
        super().__init__(("🏴‍☠️", "🦜", "💣", "💎", "⚓", self._scatter))

    def evaluate(
        self, symbols: tuple[str, str, str], bet: int, jackpot_balance: int
    ) -> tuple[int, str, int, dict[str, int]]:
        scatter_count = symbols.count(self._scatter)
        if scatter_count == 3:
            return (0, "Вы нашли карту сокровищ! Запускаются 10 бесплатных вращений!", 0, {"free_spins": 10})

        if symbols[0] == symbols[1] == symbols[2]:
            multiplier = {"🏴‍☠️": 30, "🦜": 20, "💣": 15, "💎": 10, "⚓": 6}.get(symbols[0], 8)
            winnings = bet * multiplier
            return winnings, f"Три {symbols[0]}! Вы выиграли {winnings} фишек.", 0, {}

        unique = len(set(symbols))
        if unique == 2:
            counts = {symbol: symbols.count(symbol) for symbol in set(symbols) if symbol != self._scatter}
            for symbol, count in counts.items():
                if count == 2:
                    winnings = bet * 2
                    return winnings, f"Пара {symbol}! Вы выиграли {winnings} фишек.", 0, {}

        return 0, "Шторма бушуют – пока без выигрыша!", 0, {}

    def supports_jackpot(self) -> bool:
        return False
