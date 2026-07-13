"""Telegram notifier for the bezkolejki watcher: bot commands (/status,
/pause, /resume, /test), the Confirm/Release button flow, and outbound
message sending. Extracted verbatim from bot.py — see CLAUDE.md."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import Config, WARSAW_TZ
from constants import CONFIRM_BUTTON_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from watcher import Watcher

logger = logging.getLogger("bezkolejki_bot")


# =============================================================================
# Telegram notifier
# =============================================================================

class TelegramNotifier:
    def __init__(self, config: Config):
        self.config = config
        self.app: Optional[Application] = None
        self.paused = False
        self.last_check_summary = "never"
        self.last_check_time: Optional[datetime] = None
        self.watcher_ref: Optional["Watcher"] = None  # set by Watcher so /status can show reliability stats
        # single-flight confirm/release state
        self._pending_confirm: Optional[asyncio.Future] = None
        self._pending_lock = asyncio.Lock()

    async def start(self):
        if not self.config.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        self.app = Application.builder().token(self.config.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("test", self._cmd_test))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")

    async def stop(self):
        if self.app:
            try:
                await self.app.updater.stop()
            except Exception:
                pass
            try:
                await self.app.stop()
            except Exception:
                pass
            try:
                await self.app.shutdown()
            except Exception:
                pass

    def _is_authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and str(chat.id) == str(self.config.telegram_chat_id)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        state = "PAUSED" if self.paused else "running"
        last = self.last_check_time.strftime("%Y-%m-%d %H:%M:%S %Z") if self.last_check_time else "never"
        lines = [
            f"Status: {state}",
            f"Last check: {last}",
            f"Last result: {self.last_check_summary}",
        ]
        w = self.watcher_ref
        if w is not None:
            now = datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()
            health = "⚠️ STALE (can't check)" if w.stale_alerted else "✅ healthy"
            lines += [
                f"Health: {health}",
                f"Uptime: {w._fmt_duration(now - w.started_at)}",
                f"Checks OK: {w.total_successful_checks}/{w.total_checks}",
                f"Slot hits: {w.total_slots_found}",
                f"Mode: {w.effective_mode} | captcha: {w.client._captcha_provider}",
            ]
        await update.message.reply_text("\n".join(lines))

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self.paused = True
        await update.message.reply_text("Paused. Polling will skip cycles until /resume.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self.paused = False
        await update.message.reply_text("Resumed.")

    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("Test OK - bot is alive and listening.")

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None or not self._is_authorized(update):
            if query:
                await query.answer()
            return
        await query.answer()
        data = query.data
        async with self._pending_lock:
            fut = self._pending_confirm
        if fut is not None and not fut.done():
            if data == "confirm":
                fut.set_result("confirm")
                await query.edit_message_reply_markup(reply_markup=None)
            elif data == "release":
                fut.set_result("release")
                await query.edit_message_reply_markup(reply_markup=None)
        else:
            await query.edit_message_reply_markup(reply_markup=None)

    async def send(self, text: str, reply_markup=None) -> bool:
        """Returns True if the message was delivered, False otherwise."""
        if not self.app:
            logger.warning("Telegram app not started; cannot send: %s", text)
            return False
        try:
            await self.app.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=False,
            )
            return True
        except Exception as e:  # TelegramError and anything else — never raise
            logger.error("Failed to send Telegram message: %s", e)
            return False

    async def ask_confirm_or_release(self, summary_text: str) -> str:
        """
        Send a message with Confirm/Release buttons and wait (up to
        CONFIRM_BUTTON_TIMEOUT_SECONDS) for a button press. Returns
        'confirm', 'release', 'timeout', or 'send_failed' (prompt never
        delivered — caller should not silently burn the full timeout).
        """
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Potwierdz", callback_data="confirm"),
                InlineKeyboardButton("❌ Zwolnij", callback_data="release"),
            ]
        ])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        async with self._pending_lock:
            self._pending_confirm = fut
        if not await self.send(summary_text, reply_markup=keyboard):
            async with self._pending_lock:
                self._pending_confirm = None
            logger.error("Confirm prompt failed to send — not waiting for a button.")
            return "send_failed"
        try:
            result = await asyncio.wait_for(fut, timeout=CONFIRM_BUTTON_TIMEOUT_SECONDS)
            return result
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            async with self._pending_lock:
                self._pending_confirm = None
