from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from telegram import Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

from .config import Settings
from .database import CasinoDatabase, User
from .env import load_dotenv
from .slots import FruitMachine, PharaohMachine, SlotMachine


logger = logging.getLogger(__name__)


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
        self._slot_lock = asyncio.Lock()
        self._rng = random.Random()
        self._slot_machines: dict[str, SlotMachine] = {}
        self._default_slot_key = "fruit"
        self._configure_machines()

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("start_casino", self.start_casino))
        application.add_handler(CommandHandler("balance", self.balance))
        application.add_handler(CommandHandler(["top", "leaderboard"], self.leaderboard))
        application.add_handler(CommandHandler("daily", self.daily))
        application.add_handler(CommandHandler("give", self.give))
        application.add_handler(CommandHandler(["slots", "s"], self.slots))
        application.add_handler(ChatMemberHandler(self.welcome_new_chat, ChatMemberHandler.MY_CHAT_MEMBER))

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

        args = list(context.args)
        if args and args[0].lower() in {"help", "?"}:
            await self._safe_reply(message, self._build_slots_help(), reply=False)
            return

        try:
            machine_key, bet_arg = self._parse_slot_arguments(args)
        except ValueError:
            help_text = self._build_slots_help()
            await self._safe_reply(
                message,
                "Неизвестный автомат. Используйте `/slots help` для справки.\n\n" + help_text,
                reply=False,
            )
            return
        async with self._slot_lock:
            machine = self._slot_machines.get(machine_key, self._slot_machines[self._default_slot_key])

            user = await with_db(self.db.get_user, tg_user.id)
            if not user:
                await self._safe_reply(message, "Сначала зарегистрируйтесь командой /start_casino.")
                return
            if user.balance <= 0:
                await self._safe_reply(message, "На вашем счету нет фишек. Пополните баланс командой /daily или переводом.")
                return
            await self._sync_username(tg_user, user)

            bet = self._resolve_bet(user.balance, bet_arg)
            if bet is None:
                await self._safe_reply(message, "Ставка должна быть положительным числом.")
                return

            try:
                balance_after_bet = await with_db(self.db.adjust_balance, tg_user.id, -bet)
            except ValueError:
                await self._safe_reply(message, "Недостаточно фишек для этой ставки.")
                return

            spin_message = await self._safe_reply(message, f"🎰 {machine.title}: вращаем барабаны...", reply=False)
            frame_delay = 0.9
            if spin_message:
                for _ in range(3):
                    await asyncio.sleep(frame_delay)
                    temp_symbols = [self._rng.choice(machine.reel) for _ in range(3)]
                    if not await self._safe_edit(spin_message, f"[ {' | '.join(temp_symbols)} ]"):
                        spin_message = None
                        break
            else:
                await asyncio.sleep(frame_delay * 3)

            outcome = machine.spin(bet, self._rng)
            if outcome.winnings:
                new_balance = await with_db(self.db.adjust_balance, tg_user.id, outcome.winnings)
            else:
                new_balance = balance_after_bet

            final_text = f"{outcome.message}\nВаш баланс: {new_balance} фишек."
            if spin_message and await self._safe_edit(spin_message, final_text):
                return
            await self._safe_reply(message, final_text)

    async def welcome_new_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_member = update.my_chat_member
        if chat_member is None:
            return
        new_status = chat_member.new_chat_member
        old_status = chat_member.old_chat_member
        if not new_status or new_status.user.id != context.bot.id:
            return
        chat = chat_member.chat
        if chat.type not in {"group", "supergroup"}:
            return
        if old_status and old_status.status not in {"left", "kicked"}:
            return
        commands = (
            "Добро пожаловать в чат-казино! 🤖\n"
            "Доступные команды:\n"
            "• /start_casino — регистрация и стартовый бонус\n"
            "• /balance — посмотреть баланс\n"
            "• /daily — ежедневный бонус\n"
            "• /slots <ставка> — сыграть в слоты\n"
            "• /give <сумма> @username — перевести фишки\n"
            "• /top — таблица лидеров"
        )
        await context.bot.send_message(chat.id, commands)

    def _configure_machines(self) -> None:
        fruit = FruitMachine(self.settings.slot_reel, self.settings.special_payouts)
        pharaoh = PharaohMachine()
        self._slot_machines = {
            fruit.key: fruit,
            pharaoh.key: pharaoh,
        }
        self._default_slot_key = fruit.key

    def _parse_slot_arguments(self, args: list[str]) -> tuple[str, str | None]:
        machine_key = self._default_slot_key
        bet_arg: str | None = None
        if not args:
            return machine_key, None

        first = args[0].lower()
        if first in self._slot_machines:
            machine_key = first
            if len(args) > 1:
                bet_arg = args[1]
        else:
            bet_arg = args[0]
            if bet_arg:
                try:
                    int(bet_arg)
                except ValueError as exc:
                    raise ValueError(first) from exc
        return machine_key, bet_arg

    def _resolve_bet(self, balance: int, bet_arg: str | None) -> int | None:
        if bet_arg is not None:
            try:
                bet = int(bet_arg)
            except ValueError:
                return None
            return bet if bet > 0 else None

        auto_bet = int(balance * 0.05)
        bet = max(1, min(1000, auto_bet if auto_bet > 0 else 1))
        return bet if bet > 0 else None

    def _build_slots_help(self) -> str:
        lines = ["🎰 Зал игровых автоматов:", ""]
        for machine in self._slot_machines.values():
            lines.append(
                f"• {machine.title} (`/slots {machine.key}`) — {machine.description}"
            )
        lines.append("")
        lines.append(
            "Используйте `/slots <автомат> <ставка>` или кратко `/s <автомат> <ставка>`."
        )
        lines.append(
            "Если автомат не указан, используется Фруктовый Коктейль. Если ставка не указана — 5% от баланса (мин. 1, макс. 1000)."
        )
        return "\n".join(lines)

    async def _safe_reply(self, message, text: str, *, reply: bool = True):
        for attempt in range(3):
            try:
                return await message.reply_text(text, quote=reply)
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.1)
            except TelegramError as exc:
                logger.debug("Failed to send reply: %s", exc)
                break
        return None

    async def _safe_edit(self, message, text: str) -> bool:
        for attempt in range(3):
            try:
                await message.edit_text(text)
                return True
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.1)
            except TelegramError as exc:
                logger.debug("Failed to edit message: %s", exc)
                break
        return False


def build_application(
    token: str,
    db_path: str | None = None,
    settings: Settings | None = None,
) -> Application:
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
