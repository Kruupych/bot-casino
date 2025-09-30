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
from .machine_factory import MachineFactory
from .slots import SlotMachine


logger = logging.getLogger(__name__)

WIN_BOOST_EFFECT = "win_boost"
CREDIT_LINE_EFFECT = "credit_line"
ANALYTICS_EFFECT = "analytics_subscription"


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
        self._shop_items = {item["id"]: item for item in settings.shop_items}
        self._title_items = {item_id: item for item_id, item in self._shop_items.items() if item.get("type") == "title"}
        self._icon_items = {item_id: item for item_id, item in self._shop_items.items() if item.get("type") == "balance_icon"}
        self._credit_item_id = next(
            (item_id for item_id, item in self._shop_items.items() if item.get("type") == "credit_line"),
            None,
        )
        credit_item = self._shop_items.get(self._credit_item_id) if self._credit_item_id else None
        self._credit_limit = int(credit_item.get("credit_limit", 0)) if credit_item else 0
        self._win_boost_item_id = next(
            (item_id for item_id, item in self._shop_items.items() if item.get("type") == "win_boost"),
            None,
        )
        self._analytics_item_id = next(
            (
                item_id
                for item_id, item in self._shop_items.items()
                if item.get("type") == "analytics_subscription"
            ),
            None,
        )

    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler("start_casino", self.start_casino))
        application.add_handler(CommandHandler("balance", self.balance))
        application.add_handler(CommandHandler(["top", "leaderboard"], self.leaderboard))
        application.add_handler(CommandHandler("daily", self.daily))
        application.add_handler(CommandHandler("give", self.give))
        application.add_handler(CommandHandler(["slots", "s"], self.slots))
        application.add_handler(CommandHandler(["jackpot", "jp"], self.jackpot))
        application.add_handler(CommandHandler("shop", self.shop))
        application.add_handler(CommandHandler("inventory", self.inventory))
        application.add_handler(CommandHandler("buy", self.buy))
        application.add_handler(CommandHandler("use", self.use_item))
        application.add_handler(CommandHandler("stats", self.stats))
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
        title, icon = await self._get_display_attributes(tg_user.id)
        title_text = f" ({title})" if title else ""
        icon_text = f"{icon} " if icon else ""
        await update.message.reply_text(
            f"👤 {display_name}{title_text}, ваш баланс: {icon_text}{user.balance} фишек."
        )

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
            title, icon = await self._get_display_attributes(user.telegram_id)
            title_text = f" ({title})" if title else ""
            icon_text = f"{icon} " if icon else ""
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx)
            prefix = f"{medal} " if medal else f"{idx}. "
            lines.append(f"{prefix}{name}{title_text} - {icon_text}{user.balance} фишек")
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

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None:
            return
        if not self._shop_items:
            await self._safe_reply(message, "Магазин пока пуст.")
            return
        owned_map: dict[int, int] = {}
        active_title_id: int | None = None
        active_icon_id: int | None = None
        credit_state = None
        analytics_state = None
        now = int(time.time())
        if tg_user is not None:
            owned_pairs = await with_db(self.db.get_inventory, tg_user.id)
            owned_map = {item_id: qty for item_id, qty in owned_pairs}
            profile = await with_db(self.db.get_profile, tg_user.id)
            active_title_id = profile.get("title_id") if profile else None
            active_icon_id = profile.get("balance_icon_id") if profile else None
            credit_state = await self._get_credit_line_state(tg_user.id)
            analytics_state = await self._get_analytics_access(tg_user.id)
        categories = (
            ("title", "🎖 Титулы"),
            ("balance_icon", "💠 Иконки баланса"),
            ("credit_line", "🏦 Кредитные услуги"),
            ("win_boost", "🔮 Временные бусты"),
            ("analytics_subscription", "📊 Аналитика"),
        )
        lines = ["🛍 Магазин статуса и привилегий:", ""]
        for key, label in categories:
            items = [item for item in self._shop_items.values() if item.get("type") == key]
            if not items:
                continue
            lines.append(label + ":")
            for item in sorted(items, key=lambda i: i.get("price", 0)):
                suffix = ""
                if key == "balance_icon" and item.get("value"):
                    suffix = f" ({item['value']})"
                if key == "credit_line" and item.get("credit_limit"):
                    suffix = f" (лимит {int(item['credit_limit'])} фишек)"
                if key == "win_boost":
                    duration = int(item.get("duration_seconds", 0))
                    minutes = duration // 60 if duration else 0
                    multiplier = item.get("multiplier")
                    parts: list[str] = []
                    if minutes:
                        parts.append(f"{minutes} мин")
                    if multiplier:
                        parts.append(f"x{multiplier:.2f}".rstrip("0").rstrip("."))
                    if parts:
                        suffix = f" ({', '.join(parts)})"
                price = int(item.get("price", 0))
                status_parts: list[str] = []
                if tg_user is not None:
                    item_id = item["id"]
                    owned_qty = owned_map.get(item_id, 0)
                    item_type = item.get("type")
                    if item_type in {"title", "balance_icon"} and owned_qty:
                        is_active = (
                            item_type == "title" and item_id == active_title_id
                        ) or (
                            item_type == "balance_icon" and item_id == active_icon_id
                        )
                        status_parts.append("активно" if is_active else "куплено")
                    elif owned_qty:
                        status_parts.append(f"есть {owned_qty} шт.")
                    if item_type == "credit_line" and credit_state:
                        limit = int(credit_state.get("limit", self._credit_limit))
                        status_parts.append(f"активировано (лимит {limit} фишек)")
                    if item_type == "analytics_subscription" and analytics_state:
                        remaining = analytics_state.get("expires_at", 0) - now
                        if remaining > 0:
                            status_parts.append(
                                f"активна ещё {format_timespan(remaining)}"
                            )
                    elif item_type == "analytics_subscription" and owned_qty:
                        status_parts.append(f"есть {owned_qty} шт.")
                    elif owned_qty and item.get("stackable"):
                        status_parts.append(f"есть {owned_qty} шт.")
                line = f"[{item['id']}] {item['name']}{suffix} — {price} фишек"
                if status_parts:
                    line += " — " + ", ".join(status_parts)
                lines.append(line)
            lines.append("")
        await self._safe_reply(message, "\n".join(line for line in lines if line), reply=False)

    async def inventory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return
        owned = await with_db(self.db.get_inventory, tg_user.id)
        if not owned:
            await self._safe_reply(message, "Ваш инвентарь пуст. Загляните в /shop.")
            return
        profile = await with_db(self.db.get_profile, tg_user.id)
        active_title = profile.get("title_id")
        active_icon = profile.get("balance_icon_id")
        title_lines: list[str] = []
        icon_lines: list[str] = []
        credit_lines: list[str] = []
        boost_lines: list[str] = []
        analytics_lines: list[str] = []
        active_boost = await self._get_active_win_boost(tg_user.id)
        credit_state = await self._get_credit_line_state(tg_user.id)
        analytics_state = await self._get_analytics_access(tg_user.id)
        for item_id, quantity in owned:
            item = self._shop_items.get(item_id)
            if not item:
                continue
            entry = f"[{item_id}] {item['name']}"
            if quantity > 1:
                entry += f" ×{quantity}"
            if item.get("type") == "title":
                if item_id == active_title:
                    entry += " (активно)"
                title_lines.append(entry)
            elif item.get("type") == "balance_icon":
                if item.get("value"):
                    entry += f" ({item['value']})"
                if item_id == active_icon:
                    entry += " (активно)"
                icon_lines.append(entry)
            elif item.get("type") == "credit_line":
                limit = item.get("credit_limit")
                if limit:
                    entry += f" (лимит {int(limit)} фишек)"
                if credit_state:
                    entry += " (активно)"
                credit_lines.append(entry)
            elif item.get("type") == "win_boost":
                if active_boost and active_boost.get("item_id") == item_id:
                    remaining = active_boost.get("expires_at", 0) - int(time.time())
                    if remaining > 0:
                        entry += f" (активно ещё {format_timespan(remaining)})"
                boost_lines.append(entry)
            elif item.get("type") == "analytics_subscription":
                if analytics_state:
                    remaining = analytics_state.get("expires_at", 0) - int(time.time())
                    if remaining > 0:
                        entry += f" (активна ещё {format_timespan(remaining)})"
                analytics_lines.append(entry)
        if credit_state and not credit_lines:
            limit = int(credit_state.get("limit", self._credit_limit))
            credit_lines.append(f"Активная кредитная линия (лимит {limit} фишек)")
        if analytics_state and not analytics_lines:
            remaining = analytics_state.get("expires_at", 0) - int(time.time())
            if remaining > 0:
                analytics_lines.append(
                    f"Подписка «Инсайдер» активна ещё {format_timespan(remaining)}"
                )
        lines = ["🎒 Ваши предметы:", ""]
        if title_lines:
            lines.append("🎖 Титулы:")
            lines.extend(title_lines)
            lines.append("")
        if icon_lines:
            lines.append("💠 Иконки баланса:")
            lines.extend(icon_lines)
            lines.append("")
        if credit_lines:
            lines.append("🏦 Кредитные услуги:")
            lines.extend(credit_lines)
            lines.append("")
        if boost_lines:
            lines.append("🔮 Временные бусты:")
            lines.extend(boost_lines)
            lines.append("")
        if analytics_lines:
            lines.append("📊 Аналитика:")
            lines.extend(analytics_lines)
            lines.append("")
        lines.append("Используйте /use <ID> для активации или /use reset_title /use reset_icon для сброса.")
        if boost_lines:
            lines.append("Активация амулета расходует один предмет и действует ограниченное время.")
        if analytics_lines:
            lines.append("Подписка «Инсайдер» даёт доступ к /stats на время действия.")
        await self._safe_reply(message, "\n".join(line for line in lines if line), reply=False)

    async def buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return
        if not context.args:
            await self._safe_reply(message, "Используйте: /buy <ID товара>.")
            return
        try:
            item_id = int(context.args[0])
        except ValueError:
            await self._safe_reply(message, "ID товара должен быть числом.")
            return
        item = self._shop_items.get(item_id)
        if not item:
            await self._safe_reply(message, "Товар с таким ID не найден.")
            return
        user = await with_db(self.db.get_user, tg_user.id)
        if not user:
            await self._safe_reply(message, "Сначала зарегистрируйтесь командой /start_casino.")
            return
        stackable = bool(item.get("stackable"))
        if not stackable and await with_db(self.db.has_item, tg_user.id, item_id):
            await self._safe_reply(message, "Этот предмет уже в вашем инвентаре.")
            return
        price = int(item.get("price", 0))
        try:
            new_balance = await with_db(self.db.adjust_balance, tg_user.id, -price)
        except ValueError:
            await self._safe_reply(message, "Недостаточно фишек для покупки.")
            return
        await with_db(
            self.db.add_item_to_inventory,
            tg_user.id,
            item_id,
            stackable=stackable,
        )
        quantity = await with_db(self.db.get_item_quantity, tg_user.id, item_id)
        _, icon = await self._get_display_attributes(tg_user.id)
        lines = [
            f"Покупка оформлена! Вы приобрели «{item['name']}» за {price} фишек.",
        ]
        if stackable and quantity:
            lines.append(f"Всего в наличии: {quantity} шт.")
        lines.append(self._format_balance_line(new_balance, icon))
        await self._safe_reply(message, "\n".join(lines), reply=False)

    async def use_item(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return
        if not context.args:
            await self._safe_reply(
                message,
                "Используйте: /use <ID>, /use reset_title или /use reset_icon.",
            )
            return
        arg = context.args[0].lower()
        if arg in {"reset_title", "title_off"}:
            await with_db(self.db.set_active_title, tg_user.id, None)
            await self._safe_reply(message, "Титул сброшен.")
            return
        if arg in {"reset_icon", "icon_off"}:
            await with_db(self.db.set_active_icon, tg_user.id, None)
            await self._safe_reply(message, "Иконка баланса сброшена.")
            return
        try:
            item_id = int(arg)
        except ValueError:
            await self._safe_reply(message, "ID товара должен быть числом.")
            return
        item = self._shop_items.get(item_id)
        if not item:
            await self._safe_reply(message, "Предмет не найден.")
            return
        if not await with_db(self.db.has_item, tg_user.id, item_id):
            await self._safe_reply(message, "Сначала купите этот предмет в /shop.")
            return
        item_type = item.get("type")
        if item_type == "title":
            await with_db(self.db.set_active_title, tg_user.id, item_id)
            await self._safe_reply(message, f"Титул «{item['name']}» активирован.")
        elif item_type == "balance_icon":
            await with_db(self.db.set_active_icon, tg_user.id, item_id)
            await self._safe_reply(
                message,
                f"Иконка баланса установлена на {item.get('value', '')}.",
            )
        elif item_type == "credit_line":
            current_credit = await self._get_credit_line_state(tg_user.id)
            if current_credit:
                limit = int(current_credit.get("limit", self._credit_limit))
                await self._safe_reply(
                    message,
                    (
                        "Кредитная линия уже активирована. "
                        f"Текущий лимит: {limit} фишек."
                    ),
                )
                return
            consumed = await with_db(self.db.consume_item, tg_user.id, item_id)
            if not consumed:
                await self._safe_reply(message, "В вашем инвентаре нет доступных кредитных линий.")
                return
            limit = int(item.get("credit_limit", self._credit_limit))
            await with_db(
                self.db.set_effect,
                tg_user.id,
                CREDIT_LINE_EFFECT,
                item_id=item_id,
                expires_at=0,
                value=float(limit),
            )
            await self._safe_reply(
                message,
                (
                    "Кредитная линия активирована. "
                    f"Вы можете один раз уйти в минус до {limit} фишек. Пока баланс отрицательный, новые ставки невозможны."
                ),
            )
        elif item_type == "win_boost":
            duration = max(0, int(item.get("duration_seconds", 0)))
            multiplier = float(item.get("multiplier", 1.0))
            if multiplier <= 1.0 or duration == 0:
                await self._safe_reply(message, "Этот предмет сейчас не может быть активирован.")
                return
            consumed = await with_db(self.db.consume_item, tg_user.id, item_id)
            if not consumed:
                await self._safe_reply(message, "В вашем инвентаре нет доступных амулетов.")
                return
            expires_at = int(time.time()) + duration
            await with_db(
                self.db.set_effect,
                tg_user.id,
                WIN_BOOST_EFFECT,
                item_id=item_id,
                expires_at=expires_at,
                value=multiplier,
            )
            bonus_pct = int(round((multiplier - 1.0) * 100))
            minutes = duration // 60
            await self._safe_reply(
                message,
                (
                    "Амулет удачи активирован! "
                    f"В течение {minutes} мин выигрыши увеличены на {bonus_pct}%"
                    "."
                ),
            )
        elif item_type == "analytics_subscription":
            duration = max(0, int(item.get("duration_seconds", 0)))
            if duration == 0:
                await self._safe_reply(message, "Подписку пока нельзя активировать.")
                return
            consumed = await with_db(self.db.consume_item, tg_user.id, item_id)
            if not consumed:
                await self._safe_reply(message, "В вашем инвентаре нет активируемых подписок.")
                return
            current = await self._get_analytics_access(tg_user.id)
            now = int(time.time())
            base_expiry = current.get("expires_at", now) if current else now
            if base_expiry < now:
                base_expiry = now
            new_expiry = base_expiry + duration
            await with_db(
                self.db.set_effect,
                tg_user.id,
                ANALYTICS_EFFECT,
                item_id=item_id,
                expires_at=new_expiry,
                value=None,
            )
            remaining = new_expiry - now
            await self._safe_reply(
                message,
                (
                    "Подписка «Инсайдер» активирована! "
                    f"Доступ к /stats действует ещё {format_timespan(remaining)}."
                ),
            )
        else:
            await self._safe_reply(message, "Этот предмет нельзя использовать.")

    async def slots(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return

        args = list(context.args)
        if args and args[0].lower() in {"help", "?"}:
            help_text = await self._build_slots_help()
            await self._safe_reply(message, help_text, reply=False)
            return

        try:
            machine_key, bet_arg = self._parse_slot_arguments(args)
        except ValueError:
            help_text = await self._build_slots_help()
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
            await self._sync_username(tg_user, user)

            credit_state = await self._get_credit_line_state(tg_user.id)
            credit_limit = int(credit_state.get("limit", self._credit_limit)) if credit_state else 0

            if user.balance <= 0:
                if credit_state and user.balance + credit_limit > 0:
                    pass
                elif credit_state and user.balance < 0:
                    await self._safe_reply(
                        message,
                        "Баланс отрицательный. Погасите долг, чтобы снова делать ставки.",
                    )
                    return
                else:
                    await self._safe_reply(
                        message,
                        "На вашем счету нет фишек. Пополните баланс командой /daily или переводом.",
                    )
                    return

            bet = self._resolve_bet(user.balance, bet_arg)
            if bet is None:
                await self._safe_reply(message, "Ставка должна быть положительным числом.")
                return

            bonus_amount = 0
            try:
                balance_after_bet = await with_db(
                    self.db.adjust_balance,
                    tg_user.id,
                    -bet,
                    allow_overdraft=bool(credit_state),
                    overdraft_limit=credit_limit,
                )
            except ValueError:
                if credit_state:
                    max_available = user.balance + credit_limit
                    if max_available > user.balance:
                        await self._safe_reply(
                            message,
                            (
                                "Недостаточно доступного кредита для этой ставки. "
                                f"Максимум сейчас: {max_available} фишек."
                            ),
                        )
                        return
                await self._safe_reply(message, "Недостаточно фишек для этой ставки.")
                return

            credit_line_note: str | None = None
            credit_used = bool(credit_state) and balance_after_bet < 0
            if credit_used:
                await with_db(self.db.clear_effect, tg_user.id, CREDIT_LINE_EFFECT)
                credit_line_note = (
                    "Кредитная линия израсходована. Погасите долг, чтобы оформить новую."
                )
                credit_state = None

            jackpot_balance = 0
            contribution = 0
            if machine.supports_jackpot():
                contribution = machine.jackpot_contribution(bet)
                jackpot_balance = await with_db(self.db.add_to_jackpot, machine.key, contribution)

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

            outcome = machine.spin(bet, self._rng, jackpot_balance=jackpot_balance)
            new_balance = balance_after_bet
            bonus_line: str | None = None
            if outcome.winnings:
                new_balance = await with_db(
                    self.db.adjust_balance,
                    tg_user.id,
                    outcome.winnings,
                )
                bonus_amount, bonus_line = await self._apply_win_boost(
                    tg_user.id, outcome.winnings
                )
                if bonus_amount:
                    new_balance = await with_db(
                        self.db.adjust_balance,
                        tg_user.id,
                        bonus_amount,
                    )

            total_winnings = outcome.winnings + bonus_amount

            await with_db(
                self.db.record_spin,
                tg_user.id,
                machine.key,
                bet,
                total_winnings,
                False,
            )

            current_jackpot = None
            if machine.supports_jackpot():
                if outcome.jackpot_win > 0:
                    await with_db(self.db.reset_jackpot, machine.key)
                current_jackpot = await with_db(self.db.get_jackpot, machine.key)

            _, icon = await self._get_display_attributes(tg_user.id)
            final_lines = [outcome.message]
            if bonus_line:
                final_lines.append(bonus_line)
            if credit_line_note:
                final_lines.append(credit_line_note)
            final_lines.append(self._format_balance_line(new_balance, icon))
            if machine.supports_jackpot() and current_jackpot is not None:
                final_lines.append(f"Текущий джекпот: {current_jackpot} фишек")
            final_text = "\n".join(final_lines)
            if spin_message and await self._safe_edit(spin_message, final_text):
                edited_message = spin_message
            else:
                edited_message = await self._safe_reply(message, final_text)

            if outcome.free_spins > 0:
                await self._run_free_spins(
                    message,
                    machine,
                    outcome.free_spins,
                    tg_user.id,
                    self._rng,
                    base_message=edited_message,
                )

    async def jackpot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return

        jackpots: list[str] = []
        for machine in self._slot_machines.values():
            if not machine.supports_jackpot():
                continue
            amount = await with_db(self.db.get_jackpot, machine.key)
            jackpots.append(f"• {machine.title}: {amount:,} фишек".replace(",", " "))

        if not jackpots:
            await self._safe_reply(message, "Сейчас нет активных прогрессивных джекпотов.")
            return

        text = "\n".join(["💰 Активные джекпоты в казино:", "", *jackpots])
        await self._safe_reply(message, text, reply=False)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        tg_user = update.effective_user
        if message is None or tg_user is None:
            return

        analytics = await self._get_analytics_access(tg_user.id)
        if not analytics:
            await self._safe_reply(
                message,
                "Эта команда доступна подписчикам «Инсайдер». Оформите подписку в /shop.",
            )
            return

        now = int(time.time())
        day_ago = now - 24 * 60 * 60
        week_ago = now - 7 * 24 * 60 * 60

        machine_stats = await with_db(self.db.machine_performance, day_ago)
        for entry in machine_stats:
            entry["net"] = entry["total_win"] - entry["total_bet"]

        hot = [entry for entry in machine_stats if entry["net"] > 0]
        cold = [entry for entry in machine_stats if entry["net"] < 0]
        hot_sorted = sorted(hot, key=lambda e: (e["net"], e["total_win"]), reverse=True)[:3]
        cold_sorted = sorted(cold, key=lambda e: (e["net"], e["total_win"]))[:3]

        best_day = await with_db(self.db.best_win, day_ago)
        best_week = await with_db(self.db.best_win, week_ago)

        totals_all = await with_db(self.db.user_totals, tg_user.id)
        totals_week = await with_db(self.db.user_totals, tg_user.id, week_ago)
        favourite = await with_db(self.db.user_favourite_machine, tg_user.id)

        lines = ["📊 Аналитика казино", ""]

        if hot_sorted:
            lines.append("🔥 Горячие автоматы (24 часа):")
            lines.extend(self._format_machine_line(entry) for entry in hot_sorted)
            lines.append("")
        if cold_sorted:
            lines.append("❄️ Холодные автоматы (24 часа):")
            lines.extend(self._format_machine_line(entry) for entry in cold_sorted)
            lines.append("")

        lines.append("🏆 Крупные выигрыши:")
        lines.append(
            "• За сутки: " + await self._format_best_win(best_day, fallback="данных нет")
        )
        lines.append(
            "• За неделю: " + await self._format_best_win(best_week, fallback="данных нет")
        )
        lines.append("")

        favourite_text = "нет данных"
        if favourite:
            favourite_text = f"{self._machine_title(favourite[0])} (спинов: {favourite[1]})"

        lines.append("🎯 Ваша статистика:")
        lines.append(
            "• За всё время: ставки "
            f"{self._fmt_chips(totals_all['total_bet'])}, выигрыши {self._fmt_chips(totals_all['total_win'])},"
            f" нетто {self._fmt_delta(totals_all['total_win'] - totals_all['total_bet'])}"
        )
        lines.append(
            "• За неделю: ставки "
            f"{self._fmt_chips(totals_week['total_bet'])}, выигрыши {self._fmt_chips(totals_week['total_win'])},"
            f" нетто {self._fmt_delta(totals_week['total_win'] - totals_week['total_bet'])}"
        )
        lines.append(f"• Любимый автомат: {favourite_text}")

        text = "\n".join(line for line in lines if line)
        await self._safe_reply(message, text, reply=False)

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
        factory = MachineFactory(self.settings)
        machines = factory.create_all()
        self._slot_machines = machines
        self._default_slot_key = next(iter(machines))

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

    async def _build_slots_help(self) -> str:
        lines = ["🎰 Зал игровых автоматов:", ""]
        for machine in self._slot_machines.values():
            line = f"• {machine.title} (`/slots {machine.key}`) — {machine.description}"
            if machine.supports_jackpot():
                jackpot_amount = await with_db(self.db.get_jackpot, machine.key)
                line += f" (джекпот: {jackpot_amount} фишек)"
            lines.append(line)
        lines.append("")
        lines.append(
            "Используйте `/slots <автомат> <ставка>` или кратко `/s <автомат> <ставка>`."
        )
        lines.append(
            "Если автомат не указан, используется Фруктовый Коктейль. Если ставка не указана — 5% от баланса (мин. 1, макс. 1000)."
        )
        return "\n".join(lines)

    async def _get_display_attributes(self, telegram_id: int) -> tuple[str | None, str | None]:
        profile = await with_db(self.db.get_profile, telegram_id)
        title = None
        icon = None
        title_id = profile.get("title_id") if profile else None
        icon_id = profile.get("balance_icon_id") if profile else None
        if title_id:
            item = self._title_items.get(title_id)
            if item:
                title = item.get("name")
        if icon_id:
            item = self._icon_items.get(icon_id)
            if item:
                icon = item.get("value") or ""
        return title, icon

    async def _get_credit_line_state(self, telegram_id: int) -> dict[str, int] | None:
        effect = await with_db(self.db.get_effect, telegram_id, CREDIT_LINE_EFFECT)
        if not effect:
            return None
        limit_raw = effect.get("value")
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = self._credit_limit
        limit = max(0, limit)
        return {"limit": limit}

    async def _get_analytics_access(self, telegram_id: int) -> dict[str, int] | None:
        effect = await with_db(self.db.get_effect, telegram_id, ANALYTICS_EFFECT)
        if not effect:
            return None
        expires_at_raw = effect.get("expires_at")
        expires_at = int(expires_at_raw) if expires_at_raw else 0
        if expires_at <= int(time.time()):
            await with_db(self.db.clear_effect, telegram_id, ANALYTICS_EFFECT)
            return None
        return {"expires_at": expires_at, "item_id": effect.get("item_id")}

    async def _get_active_win_boost(self, telegram_id: int) -> dict[str, float | int] | None:
        if not self._win_boost_item_id:
            return None
        effect = await with_db(self.db.get_effect, telegram_id, WIN_BOOST_EFFECT)
        if not effect:
            return None
        expires_at_raw = effect.get("expires_at")
        expires_at = int(expires_at_raw) if expires_at_raw else 0
        if expires_at <= int(time.time()):
            await with_db(self.db.clear_effect, telegram_id, WIN_BOOST_EFFECT)
            return None
        multiplier_raw = effect.get("value")
        try:
            multiplier = float(multiplier_raw)
        except (TypeError, ValueError):
            multiplier = 1.0
        multiplier = max(1.0, multiplier)
        item_id_raw = effect.get("item_id")
        try:
            item_id_val = int(item_id_raw)
        except (TypeError, ValueError):
            item_id_val = self._win_boost_item_id
        return {
            "item_id": item_id_val,
            "expires_at": expires_at,
            "multiplier": multiplier,
        }

    async def _apply_win_boost(self, telegram_id: int, base_winnings: int) -> tuple[int, str | None]:
        if base_winnings <= 0:
            return 0, None
        boost = await self._get_active_win_boost(telegram_id)
        if not boost:
            return 0, None
        multiplier = float(boost.get("multiplier", 1.0))
        if multiplier <= 1.0:
            return 0, None
        bonus = int(base_winnings * (multiplier - 1.0))
        if bonus <= 0:
            bonus = 1
        remaining = boost.get("expires_at", 0) - int(time.time())
        bonus_pct = int(round((multiplier - 1.0) * 100))
        total = base_winnings + bonus
        message = f"Бонус амулета: +{bonus} фишек ({bonus_pct}%), итого {total}."
        if remaining > 0:
            message += f" Осталось {format_timespan(int(remaining))}."
        return bonus, message

    def _format_balance_line(self, balance: int, icon: str | None) -> str:
        icon_text = f"{icon} " if icon else ""
        return f"Ваш баланс: {icon_text}{balance} фишек."

    def _fmt_chips(self, amount: int) -> str:
        return f"{amount:,}".replace(",", " ")

    def _fmt_delta(self, amount: int) -> str:
        sign = "+" if amount > 0 else ""
        return f"{sign}{self._fmt_chips(amount)}"

    def _machine_title(self, key: str) -> str:
        machine = self._slot_machines.get(key)
        return machine.title if machine else key

    def _format_machine_line(self, entry: dict[str, int]) -> str:
        title = self._machine_title(entry["machine_key"])
        net = entry["net"]
        net_text = self._fmt_delta(net)
        bet_text = self._fmt_chips(entry["total_bet"])
        win_text = self._fmt_chips(entry["total_win"])
        spins = entry["spins"]
        return (
            f"• {title}: нетто {net_text} (ставки {bet_text}, выигрыши {win_text}, спинов {spins})"
        )

    async def _format_best_win(
        self,
        record: dict[str, int] | None,
        *,
        fallback: str,
    ) -> str:
        if not record:
            return fallback
        player = await with_db(self.db.get_user, record["user_id"])
        name = format_username(player, fallback=f"Игрок {record['user_id']}")
        machine_name = self._machine_title(record["machine_key"])
        winnings = self._fmt_chips(record["winnings"])
        bet = self._fmt_chips(record["bet"])
        ago = self._format_relative_time(record["timestamp"])
        return (
            f"{name} — {winnings} фишек ({machine_name}, ставка {bet}, {ago})"
        )

    def _format_relative_time(self, timestamp: int) -> str:
        delta = max(0, int(time.time()) - int(timestamp))
        return f"{format_timespan(delta)} назад"

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

    async def _run_free_spins(
        self,
        original_message,
        machine: SlotMachine,
        count: int,
        telegram_id: int,
        rng,
        base_message=None,
    ) -> None:
        total_winnings = 0
        spin_texts: list[str] = ["🏴‍☠️ Бесплатные вращения начались!", ""]
        frame_delay = 1.0

        for i in range(1, count + 1):
            temp_symbols = [rng.choice(machine.reel) for _ in range(3)]
            frame_line = f"Вращение {i}: [ {' | '.join(temp_symbols)} ]"
            spin_texts.append(frame_line)
            if base_message:
                await self._safe_edit(base_message, "\n".join(spin_texts))
            await asyncio.sleep(frame_delay)

            outcome = machine.spin(0, rng, jackpot_balance=0)
            bonus_line_text: str | None = None
            bonus_amount = 0
            if outcome.winnings:
                await with_db(self.db.adjust_balance, telegram_id, outcome.winnings)
                total_winnings += outcome.winnings
                bonus_amount, bonus_line = await self._apply_win_boost(telegram_id, outcome.winnings)
                if bonus_amount:
                    await with_db(self.db.adjust_balance, telegram_id, bonus_amount)
                    total_winnings += bonus_amount
                    bonus_line_text = bonus_line
            if "\n" in outcome.message:
                _, second_line = outcome.message.split("\n", 1)
                result_line = f"→ {second_line}"
            else:
                result_line = f"→ {outcome.message}"
            spin_texts.append(result_line)
            if bonus_line_text:
                spin_texts.append(f"→ {bonus_line_text}")
            await with_db(
                self.db.record_spin,
                telegram_id,
                machine.key,
                0,
                outcome.winnings + bonus_amount,
                True,
            )
            if base_message:
                await self._safe_edit(base_message, "\n".join(spin_texts))
            await asyncio.sleep(0.2)

        summary = f"Бесплатные вращения завершены! Общий выигрыш: {total_winnings} фишек."
        spin_texts.append("")
        spin_texts.append(summary)
        user = await with_db(self.db.get_user, telegram_id)
        if user:
            _, icon = await self._get_display_attributes(telegram_id)
            spin_texts.append(self._format_balance_line(user.balance, icon))
        final_text = "\n".join(spin_texts)
        if base_message and await self._safe_edit(base_message, final_text):
            return
        await self._safe_reply(original_message, final_text, reply=False)


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
