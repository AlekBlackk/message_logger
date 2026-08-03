"""Telethon event handlers: cache incoming messages, report deletions and edits."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from telethon import events, utils
from telethon.errors import (
    FloodWaitError,
    MediaCaptionTooLongError,
    PhotoInvalidDimensionsError,
)

import reports
from config import Config
from storage import Storage, StoredMessage

_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class MessageLogger:
    def __init__(
        self,
        client,
        storage: Storage,
        config: Config,
        log_target_id: int,
        watch_ids: set[int],
        ignore_ids: set[int],
        log: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.config = config
        self.log_target_id = log_target_id
        self.watch_ids = watch_ids
        self.ignore_ids = ignore_ids
        self.log = log or logging.getLogger(__name__)
        # get_entity per message triggers FloodWait — keep names in memory instead
        self._names: dict[int, str] = {}
        self._titles: dict[int, str] = {}
        self._usernames: dict[int, str] = {}
        self._sender_usernames: dict[int, str] = {}

    # --- wiring -----------------------------------------------------------

    def register(self) -> None:
        self.client.add_event_handler(self.on_new_message, events.NewMessage())
        self.client.add_event_handler(self.on_message_edited, events.MessageEdited())
        self.client.add_event_handler(self.on_message_deleted, events.MessageDeleted())

    # --- filtering --------------------------------------------------------

    def is_tracked(self, peer_id: int) -> bool:
        if peer_id == self.log_target_id or peer_id in self.ignore_ids:
            return False
        if self.watch_ids and peer_id not in self.watch_ids:
            return False
        return True

    # --- name cache -------------------------------------------------------

    def remember_names(self, event) -> None:
        """Fill the in-memory caches from entities Telethon ships with the update."""
        sender = getattr(event, "sender", None)
        if sender is not None and getattr(sender, "id", None) is not None:
            sender_peer_id = utils.get_peer_id(sender)
            name = utils.get_display_name(sender)
            if name:
                self._names[sender_peer_id] = name
            sender_username = getattr(sender, "username", None)
            if sender_username:
                self._sender_usernames[sender_peer_id] = sender_username
        chat = getattr(event, "chat", None)
        chat_id = getattr(event, "chat_id", None)
        if chat is not None and chat_id is not None:
            title = utils.get_display_name(chat)
            if title:
                self._titles[chat_id] = title
            username = getattr(chat, "username", None)
            if username:
                self._usernames[chat_id] = username

    def sender_name(self, sender_id: int | None) -> str | None:
        if sender_id is None:
            return None
        return self._names.get(sender_id)

    def chat_title(self, chat_id: int) -> str | None:
        return self._titles.get(chat_id)

    def chat_username(self, chat_id: int) -> str | None:
        return self._usernames.get(chat_id)

    def sender_username(self, sender_id: int | None) -> str | None:
        if sender_id is None:
            return None
        return self._sender_usernames.get(sender_id)

    def chat_label(self, chat_id: int) -> str:
        """Display title plus @username when known, falling back to the raw id."""
        label = self._titles.get(chat_id) or str(chat_id)
        username = self._usernames.get(chat_id)
        return f"{label} (@{username})" if username else label

    def seed_chat_title(self, peer_id: int, title: str | None) -> None:
        """Prime the title cache from a source other than a live event (e.g. backfill)."""
        if title:
            self._titles[peer_id] = title

    def seed_chat_username(self, peer_id: int, username: str | None) -> None:
        """Prime the username cache from a source other than a live event (e.g. backfill)."""
        if username:
            self._usernames[peer_id] = username

    # --- storing ----------------------------------------------------------

    async def store_message(
        self,
        message,
        peer_id: int,
        is_channel: bool,
        chat_title: str | None,
        *,
        overwrite: bool = True,
    ) -> None:
        from_id = getattr(message, "from_id", None)
        sender_id = utils.get_peer_id(from_id) if from_id else None
        if sender_id is None:
            sender_id = getattr(message, "sender_id", None)
        media_path, media_type = await self.capture_media(message, peer_id)
        row = StoredMessage(
            chat_id=peer_id,
            msg_id=message.id,
            is_channel=is_channel,
            chat_title=chat_title,
            sender_id=sender_id,
            sender_name=self.sender_name(sender_id),
            text=message.message or None,
            media_path=media_path,
            media_type=media_type,
            date=int(message.date.timestamp()),
            deleted=False,
        )
        self.storage.save_message(row, overwrite=overwrite)

    @staticmethod
    def _media_label(message) -> str:
        """Human-readable media kind, using Telethon's convenience properties
        (raw media class name doesn't tell a video apart from a plain file)."""
        for attr, label in (
            ("photo", "фото"),
            ("video", "видео"),
            ("video_note", "видеосообщение"),
            ("voice", "голосовое"),
            ("audio", "аудио"),
            ("sticker", "стикер"),
            ("gif", "гиф"),
            ("document", "файл"),
        ):
            if getattr(message, attr, None) is not None:
                return label
        return type(message.media).__name__

    async def capture_media(self, message, peer_id: int) -> tuple[str | None, str | None]:
        """Download media now — after deletion the file reference is dead."""
        if message.media is None:
            return None, None
        media_type = self._media_label(message)
        file = getattr(message, "file", None)
        if not self.config.download_media or file is None:
            return None, media_type
        size = getattr(file, "size", None) or 0
        if size > self.config.max_media_bytes:
            self.log.info("media %s bytes over limit, skipping download", size)
            return None, media_type
        ext = getattr(file, "ext", None) or ".bin"
        if not _SAFE_EXT_RE.match(ext):
            ext = ".bin"
        target_dir = Path(self.config.media_dir) / str(peer_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{message.id}{ext}"
        try:
            await self.client.download_media(message, file=str(target))
        except Exception:
            self.log.exception("failed to download media for message %s", message.id)
            return None, media_type
        return str(target), media_type

    # --- sending ----------------------------------------------------------

    async def send_report(self, text: str, media_path: str | None = None) -> bool:
        for attempt in (1, 2):
            try:
                if media_path and os.path.exists(media_path):
                    try:
                        await self.client.send_file(
                            self.log_target_id,
                            media_path,
                            caption=text,
                            parse_mode="html",
                        )
                    except MediaCaptionTooLongError:
                        self.log.warning(
                            "caption too long for media, sending report as text-only"
                        )
                        await self.client.send_message(
                            self.log_target_id,
                            text,
                            parse_mode="html",
                            link_preview=False,
                        )
                    except PhotoInvalidDimensionsError:
                        self.log.warning(
                            "photo dimensions rejected, resending %s as a document",
                            media_path,
                        )
                        await self.client.send_file(
                            self.log_target_id,
                            media_path,
                            caption=text,
                            parse_mode="html",
                            force_document=True,
                        )
                else:
                    await self.client.send_message(
                        self.log_target_id,
                        text,
                        parse_mode="html",
                        link_preview=False,
                    )
                return True
            except FloodWaitError as exc:
                if attempt == 2:
                    self.log.error("flood wait again (%ss), dropping report", exc.seconds)
                    return False
                self.log.warning("flood wait %ss, retrying report", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except Exception:
                self.log.exception("failed to send report")
                return False
        return False

    # --- handlers ---------------------------------------------------------

    async def on_new_message(self, event) -> None:
        try:
            peer_id = event.chat_id
            if peer_id is None or not self.is_tracked(peer_id):
                return
            if event.out and not self.config.log_outgoing:
                return
            self.remember_names(event)
            await self.store_message(
                event.message,
                peer_id=peer_id,
                is_channel=bool(event.is_channel),
                chat_title=self.chat_title(peer_id),
            )
        except Exception:
            self.log.exception("on_new_message failed")

    async def on_message_edited(self, event) -> None:
        try:
            peer_id = event.chat_id
            if peer_id is None or not self.is_tracked(peer_id):
                return
            if event.out and not self.config.log_outgoing:
                return
            self.remember_names(event)
            new_text = event.message.message or None
            previous = self.storage.find_message(peer_id, event.message.id)
            if previous is None:
                # Never saw the original (bot was offline) — just start caching it
                await self.store_message(
                    event.message,
                    peer_id=peer_id,
                    is_channel=bool(event.is_channel),
                    chat_title=self.chat_title(peer_id),
                )
                return
            if previous.text == new_text:
                # Reactions and link-preview attachments also fire MessageEdited
                return
            edit_date = getattr(event.message, "edit_date", None)
            edited_at = int((edit_date or event.message.date).timestamp())
            self.storage.add_edit(
                peer_id, event.message.id, previous.text, new_text, edited_at
            )
            self.storage.update_text(peer_id, event.message.id, new_text)
            if self.config.log_edits:
                await self.send_report(
                    reports.format_edited(
                        previous,
                        previous.text,
                        new_text,
                        self.chat_username(peer_id),
                        self.sender_username(previous.sender_id),
                    )
                )
        except Exception:
            self.log.exception("on_message_edited failed")

    async def on_message_deleted(self, event) -> None:
        chat_id = event.chat_id  # only channels and supergroups fill this in
        if chat_id is not None and not self.is_tracked(chat_id):
            return
        for msg_id in event.deleted_ids:
            try:
                stored = self.storage.find_message(chat_id, msg_id)
                if stored is None:
                    self.log.debug("deleted message %s not in cache", msg_id)
                    continue
                if stored.deleted:
                    continue  # duplicate update for the same deletion
                if not self.is_tracked(stored.chat_id):
                    continue
                self.storage.mark_deleted(stored.chat_id, stored.msg_id)
                sent = await self.send_report(
                    reports.format_deleted(
                        stored,
                        self.chat_username(stored.chat_id),
                        self.sender_username(stored.sender_id),
                    ),
                    media_path=stored.media_path,
                )
                preview = reports.truncate(stored.text, 80) or (
                    f"[{stored.media_type}]" if stored.media_type else ""
                )
                if sent:
                    self.log.info(
                        "deletion reported: chat [%s] msg %s: %r",
                        self.chat_label(stored.chat_id),
                        stored.msg_id,
                        preview,
                    )
                else:
                    self.log.warning(
                        "deletion NOT reported (send failed): chat [%s] msg %s: %r",
                        self.chat_label(stored.chat_id),
                        stored.msg_id,
                        preview,
                    )
            except Exception:
                self.log.exception("on_message_deleted failed for msg_id %s", msg_id)
