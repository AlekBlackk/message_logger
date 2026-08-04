"""Runtime commands sent to the log target: /watch, /unwatch, /list, /purge, /help."""

from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from telethon import events, utils

import reports
from config import Config
from handlers import MessageLogger
from storage import Storage

log = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/(watch|unwatch|list|purge|help)\b\s*(.*)$", re.IGNORECASE)

# Leaves headroom under Telegram's 4096-char message cap for the header line
# and HTML markup around each entry.
_LIST_CHUNK_LIMIT = reports.MAX_TEXT_LEN

# 100 years — far more than any real retention need, but small enough that
# cutoff_ts = now - days * 86400 stays well inside SQLite's INTEGER range and
# never raises OverflowError.
_PURGE_MAX_DAYS = 36500

_PURGE_USAGE = (
    "<b>/purge</b> — очистка сообщений из БД.\n"
    "/purge <дни> — за последние N дней, все чаты (dry-run)\n"
    "/purge <@chat|id> — вся история чата (dry-run). Отрицательный id "
    "(группы/каналы) распознаётся напрямую; для личных чатов используй @username\n"
    "/purge <@chat|id> <дни> — чат + последние N дней (dry-run)\n"
    "Добавь confirm в конец, чтобы реально удалить."
)

_HELP_TEXT = (
    "<b>Команды:</b>\n"
    "/watch [@username|id] — начать следить за чатом "
    "(или ответом на пересланное сообщение)\n"
    "/unwatch [@username|id] — снять чат со слежения\n"
    "/list — список отслеживаемых чатов\n"
    "/purge <дни|@chat> [дни] [confirm] — очистить сообщения из БД, "
    "подробности: /purge без аргументов\n"
    "/help — эта справка"
)

BackfillFn = Callable[[object, MessageLogger, Config, int], Awaitable[int]]


def _is_int(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


class CommandHandler:
    def __init__(
        self,
        client,
        storage: Storage,
        logger: MessageLogger,
        config: Config,
        log_target_id: int,
        backfill_fn: BackfillFn,
        account_label: str = "",
    ) -> None:
        self.client = client
        self.storage = storage
        self.logger = logger
        self.config = config
        self.log_target_id = log_target_id
        self.backfill_fn = backfill_fn
        self.account_label = account_label

    def register(self) -> None:
        self.client.add_event_handler(
            self.on_command, events.NewMessage(chats=self.log_target_id)
        )

    async def on_command(self, event) -> None:
        # LOG_TARGET need not be "me" — if it's ever a group or channel, every
        # member could otherwise issue /watch, /unwatch, /list, /purge. Only
        # messages sent by the account owner (out=True) may run a command —
        # this matters most for /purge, where a bypass means irreversible
        # data loss.
        if not getattr(event, "out", False):
            return
        text = (getattr(event.message, "message", None) or "").strip()
        match = _COMMAND_RE.match(text)
        if not match:
            return
        command, arg = match.group(1).lower(), match.group(2).strip()
        if command == "watch":
            await self._watch(event, arg)
        elif command == "unwatch":
            await self._unwatch(event, arg)
        elif command == "purge":
            await self._purge(event, arg)
        elif command == "help":
            await self._help()
        else:
            await self._list()

    async def _resolve(self, event, arg: str):
        """Return (peer_id, entity) for an explicit @username/id, or — with no
        argument — from a forwarded message the command replies to."""
        if arg:
            ref = int(arg) if _is_int(arg) else arg
            entity = await self.client.get_entity(ref)
            return utils.get_peer_id(entity), entity
        reply = await event.message.get_reply_message()
        forward = getattr(reply, "forward", None) if reply is not None else None
        if forward is None:
            raise ValueError("укажи @username/id или ответь на пересланное сообщение")
        chat_id = getattr(forward, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(forward, "sender_id", None)
        if chat_id is None:
            raise ValueError("не удалось определить исходный чат пересланного сообщения")
        entity = await self.client.get_entity(chat_id)
        return utils.get_peer_id(entity), entity

    async def _watch(self, event, arg: str) -> None:
        try:
            peer_id, entity = await self._resolve(event, arg)
        except Exception as exc:
            await self.logger.send_report(f"⚠️ /watch: {exc}")
            return
        label = utils.get_display_name(entity) or None
        self.logger.seed_chat_title(peer_id, label)
        self.logger.seed_chat_username(peer_id, getattr(entity, "username", None))
        self.storage.add_watch(peer_id, label, int(time.time()))
        self.logger.watch_ids.add(peer_id)
        cached = await self.backfill_fn(self.client, self.logger, self.config, peer_id)
        await self.logger.send_report(
            f"✅ теперь слежу за {self.logger.chat_label(peer_id)} "
            f"(<code>{peer_id}</code>), подтянуто {cached} сообщений из истории"
        )

    async def _unwatch(self, event, arg: str) -> None:
        try:
            peer_id, entity = await self._resolve(event, arg)
        except Exception as exc:
            await self.logger.send_report(f"⚠️ /unwatch: {exc}")
            return
        label = self.logger.chat_label(peer_id)
        if label == str(peer_id):
            label = utils.get_display_name(entity) or label
        self.storage.remove_watch(peer_id)
        self.logger.watch_ids.discard(peer_id)
        note = ""
        if not self.logger.watch_ids:
            note = " Список пуст — теперь слежу за всеми чатами."
        await self.logger.send_report(
            f"❌ больше не слежу за {label} (<code>{peer_id}</code>).{note}"
        )

    async def _purge(self, event, arg: str) -> None:
        tokens = arg.split()
        confirm = False
        if tokens and tokens[-1].lower() == "confirm":
            confirm = True
            tokens = tokens[:-1]

        if not tokens:
            await self.logger.send_report(_PURGE_USAGE)
            return

        chat_ref: str | None = None
        days: int | None = None
        if len(tokens) == 1:
            if _is_int(tokens[0]) and int(tokens[0]) >= 0:
                days = int(tokens[0])
            else:
                chat_ref = tokens[0]
        elif len(tokens) == 2:
            chat_ref = tokens[0]
            if not _is_int(tokens[1]):
                await self.logger.send_report(
                    "⚠️ /purge: второй аргумент должен быть числом дней"
                )
                return
            days = int(tokens[1])
        else:
            await self.logger.send_report("⚠️ /purge: слишком много аргументов")
            return

        if days is not None and days < 1:
            await self.logger.send_report(
                "⚠️ /purge: число дней должно быть >= 1"
            )
            return
        if days is not None and days > _PURGE_MAX_DAYS:
            await self.logger.send_report(
                f"⚠️ /purge: число дней должно быть <= {_PURGE_MAX_DAYS}"
            )
            return

        chat_id: int | None = None
        scope_label = "все чаты"
        if chat_ref is not None:
            try:
                chat_id, entity = await self._resolve(event, chat_ref)
            except Exception as exc:
                await self.logger.send_report(f"⚠️ /purge: {exc}")
                return
            scope_label = self.logger.chat_label(chat_id)
            if scope_label == str(chat_id):
                scope_label = utils.get_display_name(entity) or scope_label

        cutoff_ts = int(time.time()) - days * 86400 if days is not None else None
        age_desc = f", за последние {days} дн." if days is not None else ""

        count, media_count = self.storage.count_purge_candidates(cutoff_ts, chat_id)
        if count == 0:
            await self.logger.send_report(f"Нечего чистить: {scope_label}{age_desc}.")
            return

        if not confirm:
            await self.logger.send_report(
                f"⚠️ Под удаление попадёт {count} сообщений ({media_count} с медиа) "
                f"— {scope_label}{age_desc}.\n"
                "Для удаления повтори команду с confirm в конце."
            )
            return

        paths = self.storage.purge(cutoff_ts, chat_id)
        removed = 0
        for path in paths:
            p = Path(path)
            existed = p.exists()
            try:
                p.unlink(missing_ok=True)
                if existed:
                    removed += 1
                else:
                    log.warning("purge: media already missing: %s", path)
            except OSError as exc:
                log.warning("purge: cannot delete media %s: %s", path, exc)
        await self.logger.send_report(
            f"✅ Удалено {count} сообщений, медиафайлов удалено {removed} "
            f"из {media_count} — {scope_label}{age_desc}."
        )

    async def _list(self) -> None:
        rows = self.storage.list_watch()
        if not rows:
            await self.logger.send_report("Слежу за всеми чатами (список пуст).")
            return
        account_tag = f" · {html.escape(self.account_label)}" if self.account_label else ""
        entries = []
        for chat_id, label, _added_at in rows:
            title = label or self.logger.chat_title(chat_id) or str(chat_id)
            username = self.logger.chat_username(chat_id)
            display = f"{title} (@{username})" if username else title
            entries.append(
                f"— {html.escape(display)} (<code>{chat_id}</code>){account_tag}"
            )
        pages = self._paginate(entries)
        for i, page in enumerate(pages, start=1):
            header = "<b>Отслеживаемые чаты:</b>"
            if len(pages) > 1:
                header += f" ({i}/{len(pages)})"
            await self.logger.send_report("\n".join([header, *page]))

    async def _help(self) -> None:
        await self.logger.send_report(_HELP_TEXT)

    @staticmethod
    def _paginate(
        entries: list[str], limit: int = _LIST_CHUNK_LIMIT
    ) -> list[list[str]]:
        """Group entries into pages that each stay under Telegram's message
        limit — a long watch list would otherwise blow past 4096 chars and
        send_report would silently drop the whole /list reply."""
        pages: list[list[str]] = []
        page: list[str] = []
        page_len = 0
        for entry in entries:
            entry_len = len(entry) + 1  # + newline joining it to the page
            if page and page_len + entry_len > limit:
                pages.append(page)
                page = []
                page_len = 0
            page.append(entry)
            page_len += entry_len
        if page:
            pages.append(page)
        return pages
