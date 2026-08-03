"""Runtime watch-list commands: /watch, /unwatch, /list sent to the log target."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Awaitable, Callable

from telethon import events, utils

from config import Config
from handlers import MessageLogger
from storage import Storage

log = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/(watch|unwatch|list)\b\s*(.*)$", re.IGNORECASE)

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
    ) -> None:
        self.client = client
        self.storage = storage
        self.logger = logger
        self.config = config
        self.log_target_id = log_target_id
        self.backfill_fn = backfill_fn

    def register(self) -> None:
        self.client.add_event_handler(
            self.on_command, events.NewMessage(chats=self.log_target_id)
        )

    async def on_command(self, event) -> None:
        text = (getattr(event.message, "message", None) or "").strip()
        match = _COMMAND_RE.match(text)
        if not match:
            return
        command, arg = match.group(1).lower(), match.group(2).strip()
        if command == "watch":
            await self._watch(event, arg)
        elif command == "unwatch":
            await self._unwatch(event, arg)
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

    async def _list(self) -> None:
        rows = self.storage.list_watch()
        if not rows:
            await self.logger.send_report("Слежу за всеми чатами (список пуст).")
            return
        lines = ["<b>Отслеживаемые чаты:</b>"]
        for chat_id, label, _added_at in rows:
            display = label or self.logger.chat_label(chat_id)
            lines.append(f"— {html.escape(display)} (<code>{chat_id}</code>)")
        await self.logger.send_report("\n".join(lines))
