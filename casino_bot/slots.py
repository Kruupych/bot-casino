from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence


@dataclass
class SpinResult:
    symbols: tuple[str, str, str]
    winnings: int
    message: str


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

    def spin(self, bet: int, rng: random.Random | None = None) -> SpinResult:
        if rng is None:
            rng = random
        symbols = tuple(rng.choice(self._reel) for _ in range(3))  # type: ignore[arg-type]
        winnings, body = self.evaluate(symbols, bet)
        header = f"[ {' | '.join(symbols)} ]"
        return SpinResult(symbols=symbols, winnings=winnings, message=f"{header}\n{body}")

    def evaluate(self, symbols: tuple[str, str, str], bet: int) -> tuple[int, str]:
        raise NotImplementedError


class FruitMachine(SlotMachine):
    key = "fruit"
    title = "Фруктовый Коктейль"
    description = "Классический автомат с простыми правилами и быстрыми выигрышами."

    def __init__(self, reel: Sequence[str], special_payouts: dict[tuple[str, str, str], int]) -> None:
        super().__init__(reel)
        self._special_payouts = special_payouts

    def evaluate(self, symbols: tuple[str, str, str], bet: int) -> tuple[int, str]:
        payout = self._special_payouts.get(symbols)
        if payout:
            winnings = bet * payout
            if symbols == ("💎", "💎", "💎"):
                return winnings, "💥 ДЖЕКПОТ! 💥 Вы выиграли {0} фишек!".format(winnings)
            if symbols == ("🍀", "🍀", "🍀"):
                return winnings, "Удача на вашей стороне! Три клевера приносят {0} фишек.".format(winnings)
            if symbols == ("🔔", "🔔", "🔔"):
                return winnings, "🔔 Звон монет! 🔔 Вы выиграли {0} фишек.".format(winnings)
            return winnings, "Вы сорвали крупный выигрыш: {0} фишек!".format(winnings)

        if symbols[0] == symbols[1] == symbols[2]:
            winnings = bet * 5
            return winnings, "Три совпадения! Вы выиграли {0} фишек.".format(winnings)

        if len({symbols[0], symbols[1], symbols[2]}) == 2:
            winnings = bet * 2
            return winnings, "Два совпадения! Вы выиграли {0} фишек.".format(winnings)

        return 0, "Увы, в этот раз не повезло. Попробуйте еще раз!"


class PharaohMachine(SlotMachine):
    key = "pharaoh"
    title = "Золото Фараона"
    description = "Автомат с диким символом Фараона. Wild заменяет любые символы и удваивает выигрыш."

    _wild = "🗿"
    _jackpot_multiplier = 60
    _triple_payouts = {
        "🐍": 20,
        "🐞": 16,
        "👁️": 12,
        "🏺": 10,
    }
    _double_payouts = {
        "🐍": 5,
        "🐞": 4,
        "👁️": 3,
        "🏺": 2,
    }

    def __init__(self) -> None:
        super().__init__(("🐍", "🐞", "👁️", "🏺", self._wild))

    def evaluate(self, symbols: tuple[str, str, str], bet: int) -> tuple[int, str]:
        wild_count = symbols.count(self._wild)
        if wild_count == 3:
            winnings = bet * self._jackpot_multiplier
            return winnings, "👑 Три фараона! Прогрессивный джекпот приносит {0} фишек.".format(winnings)

        non_wild = [s for s in symbols if s != self._wild]
        if wild_count:
            if not non_wild:
                winnings = bet * self._jackpot_multiplier
                return winnings, "👑 Три фараона! Прогрессивный джекпот приносит {0} фишек.".format(winnings)

            best_symbol = self._choose_best_symbol(non_wild)
            matches = non_wild.count(best_symbol) + wild_count
            multiplier = 0
            if matches >= 3:
                multiplier = self._triple_payouts.get(best_symbol, 0)
            elif matches == 2:
                multiplier = self._double_payouts.get(best_symbol, 0)

            if multiplier:
                multiplier *= 2
                winnings = bet * multiplier
                if matches >= 3:
                    return winnings, (
                        "🗿 Фараон поддержал комбинацию! Три {0} приносят {1} фишек с множителем x2."
                    ).format(best_symbol, winnings)
                return winnings, (
                    "🗿 Фараон дополнил ваш выигрыш! Пара {0} приносит {1} фишек с множителем x2."
                ).format(best_symbol, winnings)

        if len(set(symbols)) == 1 and symbols[0] != self._wild:
            symbol = symbols[0]
            multiplier = self._triple_payouts.get(symbol, 0)
            if multiplier:
                winnings = bet * multiplier
                return winnings, "Три {0}! Вы выиграли {1} фишек.".format(symbol, winnings)

        counts = {s: symbols.count(s) for s in set(symbols) if s != self._wild}
        for symbol, count in counts.items():
            if count == 2:
                multiplier = self._double_payouts.get(symbol, 0)
                if multiplier:
                    winnings = bet * multiplier
                    return winnings, "Пара {0} приносит {1} фишек.".format(symbol, winnings)

        return 0, "Пески пусты. Попробуйте ещё раз!"

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
