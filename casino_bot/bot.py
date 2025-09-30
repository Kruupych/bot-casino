from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Sequence

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from .config import Settings
from .database import CasinoDatabase, User
from .env import load_dotenv


async def with_db(op, *args, **kwargs):
    return await asyncio.to_thread(op, *args, **kwargs)


def format_username(user: User | None, fallback: str | None = None) -> str:
    if user and user.username:
        return f"@{user.username}" if not user.username.startswith("@") else user.username
    if fallback:
        return fallback
    return "Игрок"


def format_timespan(seconds: int) -> str:
    parts: list[str] = []
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if secs or not parts:
        parts.append(f"{secs} сек.")
    return " ".join(parts)


class CasinoBot:
    def __init__(self, db: CasinoDatabase, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("start_casino", self.start_casino))
        application.add_handler(CommandHandler("balance", self.balance))
        application.add_handler(CommandHandler(["top", "leaderboard"], self.leaderboard))
        application.add_handler(CommandHandler("daily", self.daily))
        application.add_handler(CommandHandler("give", self.give))
        application.add_handler(CommandHandler("slots", self.slots))

    async def _sync_username(self, telegram_user, record: User | None) -> None:
        if not telegram_user or not record or not telegram_user.username:
            return
        if telegram_user.username != record.username:
            await with_db(self.db.update_username, telegram_user.id, telegram_user.username)

    async def start_casino(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg_user = update.effective_user
        if tg_user is None or update.effective_chat is None or update.message is None:
            return

        existing = await with_db(self.db.get_user, tg_user.id)
        if existing:
            await self._sync_username(tg_user, existing)
            await update.message.reply_text(
                f"Вы уже зарегистрированы. Ваш баланс: {existing.balance} фишек."
            )
            return

        await with_db(self.db.create_user, tg_user.id, tg_user.username, self.settings.starting_balance)
        await update.message.reply_text(
            (
                "Добро пожаловать в наше казино! "
                f"На ваш счет зачислено {self.settings.starting_balance} фишек. Удачи!"
            )
        )

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg_user = update.effective_user
        if tg_user is None or update.effective_chat is None or update.message is None:
            return

        user = await with_db(self.db.get_user, tg_user.id)
        if not user:
            await update.message.reply_text("Сначала зарегистрируйтесь командой /start_casino.")
            return
        await self._sync_username(tg_user, user)

        display_name = format_username(user, tg_user.full_name)
        await update.message.reply_text(f"👤 {display_name}, ваш баланс: 💰 {user.balance} фишек.")

    async def daily(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg_user = update.effective_user
        if tg_user is None or update.effective_chat is None or update.message is None:
            return

        user = await with_db(self.db.get_user, tg_user.id)
        if not user:
            await update.message.reply_text("Сначала зарегистрируйтесь командой /start_casino.")
            return
        await self._sync_username(tg_user, user)

        now = int(time.time())
        if user.last_daily_timestamp is not None:
            elapsed = now - user.last_daily_timestamp
            if elapsed < self.settings.daily_cooldown_seconds:
                remaining = self.settings.daily_cooldown_seconds - elapsed
                await update.message.reply_text(
                    f"Ежедневный бонус уже получен. Попробуйте через {format_timespan(remaining)}."
                )
                return

        new_balance = await with_db(self.db.adjust_balance, tg_user.id, self.settings.daily_bonus)
        await with_db(self.db.set_daily_timestamp, tg_user.id, now)
        await update.message.reply_text(
            (
                f"🎉 Вы получили ежедневный бонус в {self.settings.daily_bonus} фишек! "
                f"Ваш баланс: {new_balance} фишек."
            )
        )

    async def leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return

        top_users = await with_db(self.db.top_users, self.settings.leaderboard_limit)
        if not top_users:
            await update.message.reply_text("Таблица лидеров пока пуста. Станьте первым! 🎯")
            return

        lines = ["🏆 Таблица лидеров нашего казино:\n"]
        for idx, user in enumerate(top_users, start=1):
            name = format_username(user, fallback=f"Игрок {user.telegram_id}")
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx)
            prefix = f"{medal} " if medal else f"{idx}. "
            lines.append(f"{prefix}{name} - {user.balance} фишек")
        await update.message.reply_text("\n".join(lines))

    async def give(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return

        if len(context.args) < 2:
            await message.reply_text("Используйте: /give <сумма> @username")
            return

        amount_arg = context.args[0]
        recipient_arg = context.args[1]

        try:
            amount = int(amount_arg)
        except ValueError:
            await message.reply_text("Сумма должна быть положительным числом.")
            return

        if amount <= 0:
            await message.reply_text("Сумма должна быть положительным числом.")
            return

        if not recipient_arg.startswith("@"):
            await message.reply_text("Не удалось распознать получателя. Используйте формат @username.")
            return

        recipient_username = recipient_arg.removeprefix("@")

        sender = await with_db(self.db.get_user, tg_user.id)
        if not sender:
            await message.reply_text("Сначала зарегистрируйтесь командой /start_casino.")
            return
        await self._sync_username(tg_user, sender)

        recipient = await with_db(self.db.get_user_by_username, recipient_username)
        if not recipient:
            await message.reply_text("Получатель не найден или не зарегистрирован.")
            return

        if recipient.telegram_id == sender.telegram_id:
            await message.reply_text("Нельзя переводить фишки самому себе.")
            return

        try:
            sender_balance, _ = await with_db(
                self.db.transfer, sender.telegram_id, recipient.telegram_id, amount
            )
        except ValueError as exc:
            await message.reply_text(str(exc))
            return

        sender_name = format_username(sender, fallback=tg_user.full_name)
        recipient_name = format_username(recipient)

        await message.reply_text(
            (
                f"Перевод выполнен! {sender_name} отправил {amount} фишек игроку {recipient_name}.\n"
                f"Ваш новый баланс: {sender_balance} фишек."
            )
        )

    async def slots(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return

        if not context.args:
            await message.reply_text("Используйте: /slots <ставка>")
            return

        try:
            bet = int(context.args[0])
        except ValueError:
            await message.reply_text("Ставка должна быть положительным числом.")
            return

        if bet <= 0:
            await message.reply_text("Ставка должна быть положительным числом.")
            return

        user = await with_db(self.db.get_user, tg_user.id)
        if not user:
            await message.reply_text("Сначала зарегистрируйтесь командой /start_casino.")
            return
        await self._sync_username(tg_user, user)

        try:
            balance_after_bet = await with_db(self.db.adjust_balance, tg_user.id, -bet)
        except ValueError:
            await message.reply_text("Недостаточно фишек для этой ставки.")
            return

        spin_message = await message.reply_text("🎰 Крутим барабан...")
        await asyncio.sleep(0.8)

        final_symbols = [random.choice(self.settings.slot_reel) for _ in range(3)]
        for _ in range(2):
            temp_symbols = [random.choice(self.settings.slot_reel) for _ in range(3)]
            await spin_message.edit_text(f"[ {' | '.join(temp_symbols)} ]")
            await asyncio.sleep(0.5)

        await spin_message.edit_text(f"[ {' | '.join(final_symbols)} ]")

        multiplier = self._payout_multiplier(tuple(final_symbols))
        winnings = bet * multiplier
        if winnings:
            new_balance = await with_db(self.db.adjust_balance, tg_user.id, winnings)
        else:
            new_balance = balance_after_bet

        result_text = self._build_slots_result_text(final_symbols, winnings, new_balance)
        await message.reply_text(result_text)

    def _payout_multiplier(self, symbols: tuple[str, str, str]) -> int:
        special = self.settings.special_payouts.get(symbols)
        if special is not None:
            return special
        if symbols.count(symbols[0]) == 3:
            return 5
        if len({symbols[0], symbols[1], symbols[2]}) == 2:
            return 2
        return 0

    def _build_slots_result_text(self, symbols: Sequence[str], winnings: int, new_balance: int) -> str:
        header = f"[ {' | '.join(symbols)} ]"
        if winnings == 0:
            return f"{header}\nУвы, в этот раз не повезло. Попробуйте еще раз! Ваш баланс: {new_balance} фишек."
        if tuple(symbols) == ("💎", "💎", "💎"):
            return (
                f"{header}\n💥 ДЖЕКПОТ! 💥 Вы выиграли {winnings} фишек! Ваш новый баланс: {new_balance} фишек."
            )
        if tuple(symbols) == ("🍀", "🍀", "🍀"):
            return (
                f"{header}\nУдача на вашей стороне! Три клевера приносят {winnings} фишек. Баланс: {new_balance} фишек."
            )
        if tuple(symbols) == ("🔔", "🔔", "🔔"):
            return (
                f"{header}\n🔔 Звон монет! 🔔 Вы выиграли {winnings} фишек. Баланс: {new_balance} фишек."
            )
        if len(set(symbols)) == 1:
            return f"{header}\nТри совпадения! Вы выиграли {winnings} фишек. Ваш баланс: {new_balance} фишек."
        if len(set(symbols)) == 2:
            return f"{header}\nДва совпадения! Вы выиграли {winnings} фишек. Ваш баланс: {new_balance} фишек."
        return f"{header}\nВы выиграли {winnings} фишек. Ваш баланс: {new_balance} фишек."


def build_application(token: str, db_path: str | None = None, settings: Settings | None = None) -> Application:
    db_location = db_path or os.environ.get("CASINO_DB_PATH", "casino.sqlite3")
    db = CasinoDatabase(db_location)
    config = settings or Settings.from_env()

    application = ApplicationBuilder().token(token).build()
    CasinoBot(db, config).register(application)
    return application


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")
    app = build_application(token)
    app.run_polling()


__all__ = ["build_application", "main", "CasinoBot"]
