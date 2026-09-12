"""Telethon event handlers: cache incoming messages, report deletions and edits."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from telethon import events, utils
from telethon.errors import (
    FloodWaitError,
    MediaCaptionTooLongError,
    PhotoInvalidDimensionsError,
)
from telethon.tl.types import DocumentAttributeAnimated

import reports
from config import Config
from storage import Storage, StoredMessage

_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")

# Telegram accepts at most 100 ids per messages.getMessages call.
RECONCILE_CHUNK = 100
RECONCILE_PAUSE_SEC = 1.0
# A blind spot can hide hundreds of deletions; draining them all in one pass
# would trip FloodWait on the log target. The rest waits for the next pass.
RECONCILE_MAX_REPORTS = 30
# One delete event can carry hundreds of messages at once (a ban wipes every
# message the user ever sent); past this many reports the rest are marked
# deleted without a report to keep the log target from a FloodWait burst.
BATCH_REPORT_LIMIT = 10
# Telegram cascades ban/post deletions into comment/reply threads across
# multiple chunks or updates. Replies deleted within this window of their
# parent are considered collateral damage rather than intentional deletions.
CASCADE_WINDOW_SEC = 60.0
# Upper bound for waiting on in-flight media download when a deletion arrives.
DOWNLOAD_TIMEOUT_SEC = 30.0


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
        self.recent_deletions: dict[tuple[int, int], float] = {}
        self._inflight_downloads: dict[tuple[int, int], asyncio.Event] = {}

    def record_deletion(self, chat_id: int, msg_id: int) -> None:
        now = time.time()
        self.recent_deletions[(chat_id, msg_id)] = now
        cutoff = now - CASCADE_WINDOW_SEC
        self.recent_deletions = {
            k: ts for k, ts in self.recent_deletions.items() if ts >= cutoff
        }

    def is_recently_deleted(self, chat_id: int, msg_id: int) -> bool:
        now = time.time()
        ts = self.recent_deletions.get((chat_id, msg_id))
        if ts is None:
            return False
        if now - ts > CASCADE_WINDOW_SEC:
            del self.recent_deletions[(chat_id, msg_id)]
            return False
        return True

    # --- wiring -----------------------------------------------------------

    def register(self) -> None:
        self.client.add_event_handler(self.on_new_message, events.NewMessage())
        self.client.add_event_handler(self.on_message_edited, events.MessageEdited())
        self.client.add_event_handler(self.on_message_deleted, events.MessageDeleted())
        self.client.add_event_handler(self.on_chat_action, events.ChatAction())

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
        existing = self.storage.find_message(peer_id, message.id)
        if existing is not None and (not overwrite or existing.deleted):
            # A row already flagged deleted has been reported and is final:
            # catch_up replays its NewMessage update after a reconnect, and
            # re-downloading media over the copy the report was built from
            # burns rate limit for nothing.
            return
        from_id = getattr(message, "from_id", None)
        sender_id = utils.get_peer_id(from_id) if from_id else None
        if sender_id is None:
            sender_id = getattr(message, "sender_id", None)
        media_type = self._media_label(message) if message.media is not None else None
        row = StoredMessage(
            chat_id=peer_id,
            msg_id=message.id,
            is_channel=is_channel,
            chat_title=chat_title,
            sender_id=sender_id,
            sender_name=self.sender_name(sender_id),
            text=message.message or None,
            media_path=None,
            media_type=media_type,
            date=int(message.date.timestamp()),
            deleted=False,
            reply_to_msg_id=getattr(message, "reply_to_msg_id", None),
        )
        # Row first, download second: updates are dispatched concurrently, so a
        # message deleted while its media is still downloading would otherwise
        # reach on_message_deleted before it is cached — and go unreported.
        self.storage.save_message(row, overwrite=overwrite)
        if media_type is None:
            return
        download_key = (peer_id, message.id)
        if download_key in self._inflight_downloads:
            await self._inflight_downloads[download_key].wait()
            return
        event = asyncio.Event()
        self._inflight_downloads[download_key] = event
        try:
            downloaded = await self.fetch_media(message, peer_id, chat_title)
            if downloaded is None:
                return
            media_path, media_size = downloaded
            self.storage.update_media_path(peer_id, message.id, media_path, media_size)
            if self.config.max_media_total_bytes > 0:
                await self.enforce_media_quota()
        finally:
            event.set()
            self._inflight_downloads.pop(download_key, None)

    @staticmethod
    def _media_label(message) -> str:
        """Human-readable media kind, using Telethon's convenience properties
        (raw media class name doesn't tell a video apart from a plain file)."""
        for attr, label in (
            ("photo", "фото"),
            ("sticker", "стикер"),
            ("gif", "гиф"),
            ("video_note", "видеосообщение"),
            ("voice", "голосовое"),
            ("audio", "аудио"),
            ("video", "видео"),
            ("document", "файл"),
        ):
            if getattr(message, attr, None) is not None:
                return label
        return type(message.media).__name__

    @staticmethod
    def _safe_dir_name(title: str) -> str:
        """Chat title as a filesystem-safe folder name (cross-platform)."""
        name = re.sub(r'[\\/:*?"<>|]', "_", title).strip(" .")
        return name or "_"

    async def fetch_media(
        self, message, peer_id: int, chat_title: str | None = None
    ) -> tuple[str, int] | None:
        """Download media now — after deletion the file reference is dead."""
        file = getattr(message, "file", None)
        if not self.config.download_media or file is None:
            return None
        size = getattr(file, "size", None) or 0
        if size > self.config.max_media_bytes:
            self.log.info("media %s bytes over limit, skipping download", size)
            return None
        ext = getattr(file, "ext", None) or ".bin"
        if not _SAFE_EXT_RE.match(ext):
            ext = ".bin"
        title = chat_title or self.chat_title(peer_id)
        dir_name = self._safe_dir_name(title) if title else str(peer_id)
        target_dir = Path(self.config.media_dir) / dir_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{message.id}{ext}"
        try:
            await self.client.download_media(message, file=str(target))
        except Exception:
            self.log.exception("failed to download media for message %s", message.id)
            target.unlink(missing_ok=True)
            return None
        # Download races the source message's own deletion: if it dies
        # mid-transfer the file reference can go dead without Telethon
        # raising, leaving a truncated file on disk that Telegram clients
        # can't decode. A size mismatch is the only local signal we get —
        # discard it and let the report fall back to text-only.
        actual_size = target.stat().st_size if target.exists() else 0
        if actual_size == 0 or (size and actual_size != size):
            self.log.warning(
                "media for message %s incomplete after download (%s of %s bytes) "
                "— source message likely deleted mid-transfer, discarding",
                message.id,
                actual_size,
                size,
            )
            target.unlink(missing_ok=True)
            return None
        return str(target), actual_size

    async def enforce_media_quota(self) -> None:
        """Evict the oldest downloaded media until MAX_MEDIA_TOTAL_MB is met
        again. Message rows survive with their text — only the media file and
        its DB reference are dropped."""
        paths = self.storage.evict_oldest_media(self.config.max_media_total_bytes)
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                self.log.warning("quota eviction: cannot delete media %s: %s", path, exc)
        if paths:
            self.log.info(
                "media quota: evicted %s file(s) to stay under %s MB",
                len(paths),
                self.config.max_media_total_mb,
            )

    # --- reply context ------------------------------------------------------

    def reply_preview(self, chat_id: int, reply_to_msg_id: int | None) -> str | None:
        """One-line summary of the message a deleted/edited message replied
        to, for context in the report. None when there is no reply, or the
        parent was never cached (untracked chat, purged, bot offline)."""
        if not reply_to_msg_id:
            return None
        parent = self.storage.find_message(chat_id, reply_to_msg_id)
        if parent is None:
            return None
        name = parent.sender_name or "?"
        body = reports.truncate(parent.text, 120) or (
            f"[{parent.media_type}]" if parent.media_type else "без текста"
        )
        return f"{name}: {body}"

    # --- sending ----------------------------------------------------------

    async def _send_reply_media(
        self,
        media_path: str,
        *,
        media_type: str | None = None,
        author: str | None = None,
        reply_to: int | None = None,
    ) -> None:
        """Send parent message media in reply to the deletion report."""
        caption = reports.format_reply_media_caption(media_type, author)
        send_kw: dict = {
            "supports_streaming": True,
        }
        if caption is not None:
            send_kw["caption"] = caption
            send_kw["parse_mode"] = "html"
        if reply_to is not None:
            send_kw["reply_to"] = reply_to

        type_lower = (media_type or "").strip().lower()
        if type_lower in ("видеосообщение", "video_note", "кружочек"):
            send_kw["video_note"] = True
        elif type_lower in ("гиф", "gif", "animation"):
            send_kw["attributes"] = [DocumentAttributeAnimated()]
        elif type_lower in ("голосовое", "голос", "voice"):
            send_kw["voice_note"] = True

        try:
            await self.client.send_file(
                self.log_target_id,
                media_path,
                **send_kw,
            )
        except MediaCaptionTooLongError:
            self.log.warning(
                "caption too long for reply media, sending without caption"
            )
            retry_kw = dict(send_kw)
            retry_kw.pop("caption", None)
            retry_kw.pop("parse_mode", None)
            await self.client.send_file(
                self.log_target_id,
                media_path,
                **retry_kw,
            )
        except PhotoInvalidDimensionsError:
            self.log.warning(
                "photo dimensions rejected for reply media, resending %s as a document",
                media_path,
            )
            doc_kw = dict(send_kw)
            doc_kw.pop("supports_streaming", None)
            doc_kw["force_document"] = True
            try:
                await self.client.send_file(
                    self.log_target_id,
                    media_path,
                    **doc_kw,
                )
            except MediaCaptionTooLongError:
                doc_kw.pop("caption", None)
                doc_kw.pop("parse_mode", None)
                await self.client.send_file(
                    self.log_target_id,
                    media_path,
                    **doc_kw,
                )

    async def send_report(
        self,
        text: str,
        media_path: str | None = None,
        *,
        media_type: str | None = None,
        reply_media_path: str | None = None,
        reply_media_type: str | None = None,
        reply_media_author: str | None = None,
    ) -> bool:
        for attempt in (1, 2):
            try:
                sent_msg = None
                if media_path and os.path.exists(media_path):
                    send_kw: dict = {
                        "caption": text,
                        "parse_mode": "html",
                        "supports_streaming": True,
                    }
                    if media_type and media_type.lower() in ("гиф", "gif"):
                        send_kw["attributes"] = [DocumentAttributeAnimated()]
                    try:
                        sent_msg = await self.client.send_file(
                            self.log_target_id,
                            media_path,
                            **send_kw,
                        )
                    except MediaCaptionTooLongError:
                        self.log.warning(
                            "caption too long for media, sending report as text-only"
                        )
                        sent_msg = await self.client.send_message(
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
                        doc_kw = dict(send_kw)
                        doc_kw.pop("supports_streaming", None)
                        doc_kw["force_document"] = True
                        sent_msg = await self.client.send_file(
                            self.log_target_id,
                            media_path,
                            **doc_kw,
                        )
                else:
                    sent_msg = await self.client.send_message(
                        self.log_target_id,
                        text,
                        parse_mode="html",
                        link_preview=False,
                    )

                if (
                    reply_media_path
                    and os.path.exists(reply_media_path)
                    and sent_msg is not None
                ):
                    try:
                        reply_target = (
                            sent_msg[0]
                            if isinstance(sent_msg, (list, tuple)) and sent_msg
                            else sent_msg
                        )
                        reply_to_id = getattr(reply_target, "id", None) or (
                            reply_target if isinstance(reply_target, int) else None
                        )
                        await self._send_reply_media(
                            reply_media_path,
                            media_type=reply_media_type,
                            author=reply_media_author,
                            reply_to=reply_to_id,
                        )
                    except Exception:
                        self.log.exception(
                            "failed to send reply media for report: %s",
                            reply_media_path,
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
                        reply_preview=self.reply_preview(peer_id, previous.reply_to_msg_id),
                    )
                )
        except Exception:
            self.log.exception("on_message_edited failed")

    async def _wait_inflight_download(
        self, chat_id: int, msg_id: int, timeout: float | None = None
    ) -> None:
        """Wait for any active background media download for this message to
        finish before building or sending a deletion report."""
        event = self._inflight_downloads.get((chat_id, msg_id))
        if event is None:
            return
        effective_timeout = (
            timeout if timeout is not None else DOWNLOAD_TIMEOUT_SEC
        )
        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            self.log.warning(
                "timed out waiting for media download of chat [%s] msg %s",
                self.chat_label(chat_id),
                msg_id,
            )
        except Exception:
            self.log.exception(
                "error waiting for media download of chat [%s] msg %s",
                self.chat_label(chat_id),
                msg_id,
            )

    async def report_deleted(self, stored: StoredMessage, *, source: str) -> bool:
        """Send one deletion report. The row is marked deleted only once the
        report is out, so a failed send is retried by the next reconcile pass
        instead of being silently swallowed."""
        await self._wait_inflight_download(stored.chat_id, stored.msg_id)
        refreshed = self.storage.find_message(stored.chat_id, stored.msg_id)
        if refreshed is not None:
            if refreshed.deleted:
                # Already reported by a concurrent pass while waiting for download
                return False
            stored = refreshed
        reply_media_path = None
        reply_media_type = None
        reply_media_author = None
        if self.config.log_reply_media and stored.reply_to_msg_id:
            await self._wait_inflight_download(stored.chat_id, stored.reply_to_msg_id)
            recheck = self.storage.find_message(stored.chat_id, stored.msg_id)
            if recheck is not None:
                if recheck.deleted:
                    # Already reported by a concurrent pass while waiting for parent download
                    return False
                stored = recheck
            parent = self.storage.find_message(stored.chat_id, stored.reply_to_msg_id)
            if parent is not None and parent.media_path:
                reply_media_path = parent.media_path
                reply_media_type = parent.media_type
                reply_media_author = (
                    parent.sender_name
                    or self.sender_name(parent.sender_id)
                    or (parent.chat_title if parent.is_channel else None)
                    or (self.chat_title(parent.chat_id) if parent.is_channel else None)
                )

        sent = await self.send_report(
            reports.format_deleted(
                stored,
                self.chat_username(stored.chat_id),
                self.sender_username(stored.sender_id),
                reply_preview=self.reply_preview(stored.chat_id, stored.reply_to_msg_id),
            ),
            media_path=stored.media_path,
            media_type=stored.media_type,
            reply_media_path=reply_media_path,
            reply_media_type=reply_media_type,
            reply_media_author=reply_media_author,
        )
        preview = reports.truncate(stored.text, 80) or (
            f"[{stored.media_type}]" if stored.media_type else ""
        )
        if sent:
            self.storage.mark_deleted(stored.chat_id, stored.msg_id)
            self.record_deletion(stored.chat_id, stored.msg_id)
            self.log.info(
                "deletion reported (%s): chat [%s] msg %s: %r",
                source,
                self.chat_label(stored.chat_id),
                stored.msg_id,
                preview,
            )
        else:
            self.log.warning(
                "deletion NOT reported (send failed, %s): chat [%s] msg %s: %r",
                source,
                self.chat_label(stored.chat_id),
                stored.msg_id,
                preview,
            )
        return sent

    async def on_message_deleted(self, event) -> None:
        chat_id = event.chat_id  # only channels and supergroups fill this in
        if chat_id is not None and not self.is_tracked(chat_id):
            return
        candidates: list[StoredMessage] = []
        for msg_id in event.deleted_ids:
            try:
                stored = self.storage.find_message(chat_id, msg_id)
                if stored is None:
                    if chat_id is None:
                        # No chat id means DMs and basic groups share one id
                        # space, so most of these belong to untracked chats.
                        self.log.debug("deleted message %s not in cache", msg_id)
                    else:
                        self.log.warning(
                            "deleted message not in cache: chat [%s] msg %s",
                            self.chat_label(chat_id),
                            msg_id,
                        )
                    continue
                if stored.deleted:
                    continue  # duplicate update for the same deletion
                if not self.is_tracked(stored.chat_id):
                    continue
                candidates.append(stored)
            except Exception:
                self.log.exception("on_message_deleted failed for msg_id %s", msg_id)
        # Deleting a channel post or banning a user cascades into Telegram deleting
        # the whole discussion-group comment thread / replies under it (either in
        # the same batch, across multiple chunks, or for messages from previous
        # days) — those replies are collateral, not something anyone deleted on
        # purpose, so they're marked deleted silently instead of each spawning a
        # report. The tell is a reply to a deleted parent (in this batch, recently
        # deleted in an earlier chunk of the cascade, or authored by a sender whose
        # messages are wiped) from a *different* author; a banned user's replies
        # to their own earlier messages share a sender and stay reported.
        by_id = {stored.msg_id: stored for stored in candidates}
        candidates_senders = {
            stored.sender_id for stored in candidates if stored.sender_id is not None
        }
        reportable: list[StoredMessage] = []
        for stored in candidates:
            parent = (
                by_id.get(stored.reply_to_msg_id)
                if stored.reply_to_msg_id
                else None
            )
            if parent is None and stored.reply_to_msg_id:
                parent = self.storage.find_message(stored.chat_id, stored.reply_to_msg_id)

            if (
                parent is not None
                and parent.sender_id != stored.sender_id
                and (
                    parent.msg_id in by_id
                    or self.is_recently_deleted(stored.chat_id, parent.msg_id)
                    or (parent.sender_id is not None and parent.sender_id in candidates_senders)
                )
            ):
                self.storage.mark_deleted(stored.chat_id, stored.msg_id)
                self.record_deletion(stored.chat_id, stored.msg_id)
            else:
                reportable.append(stored)

        # Newest deletion first, so the freshest report sits at the top of the log.
        reportable.sort(key=lambda s: (s.date, s.msg_id), reverse=True)
        over_cap = reportable[BATCH_REPORT_LIMIT:]
        for stored in over_cap:
            try:
                self.storage.mark_deleted(stored.chat_id, stored.msg_id)
                self.record_deletion(stored.chat_id, stored.msg_id)
            except Exception:
                self.log.exception(
                    "on_message_deleted failed for msg_id %s", stored.msg_id
                )
        if over_cap:
            self.log.warning(
                "on_message_deleted: %s deletion(s) over the per-event cap of "
                "%s marked deleted without a report",
                len(over_cap),
                BATCH_REPORT_LIMIT,
            )

        for stored in reportable[:BATCH_REPORT_LIMIT]:
            try:
                await self.report_deleted(stored, source="live")
            except Exception:
                self.log.exception(
                    "on_message_deleted failed for msg_id %s", stored.msg_id
                )

    async def on_chat_action(self, event) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None or not self.is_tracked(chat_id):
                return
            if not self.config.log_joins:
                return
            joined = event.user_joined or event.user_added
            left = event.user_left or event.user_kicked
            if not joined and not left:
                return
            self.remember_names(event)
            users = await event.get_users()
            if not users:
                return
            actor = None
            if event.user_added:
                actor = await event.get_added_by()
            elif event.user_kicked:
                actor = await event.get_kicked_by()
            for user in users:
                await self._report_membership(chat_id, user, joined=joined, actor=actor)
        except Exception:
            self.log.exception("on_chat_action failed")

    async def _report_membership(self, chat_id: int, user, *, joined: bool, actor) -> None:
        name = utils.get_display_name(user) or None
        text = reports.format_membership(
            joined=joined,
            chat_title=self.chat_title(chat_id),
            chat_id=chat_id,
            chat_username=self.chat_username(chat_id),
            user_name=name,
            user_id=getattr(user, "id", None),
            user_username=getattr(user, "username", None),
            actor_name=utils.get_display_name(actor) if actor else None,
            actor_id=getattr(actor, "id", None) if actor else None,
            actor_username=getattr(actor, "username", None) if actor else None,
        )
        sent = await self.send_report(text)
        if sent:
            self.log.info(
                "%s reported: chat [%s] user %s (%s)",
                "join" if joined else "leave",
                self.chat_label(chat_id),
                name,
                getattr(user, "id", None),
            )
        else:
            self.log.warning(
                "membership change NOT reported (send failed): chat [%s] user %s (%s)",
                self.chat_label(chat_id),
                name,
                getattr(user, "id", None),
            )

    # --- reconciliation ---------------------------------------------------

    async def reconcile(self, since_ts: int) -> int:
        """Report deletions the update stream never delivered.

        Deletion updates are not guaranteed: they are lost across a
        reconnect gap, and auto-delete (TTL) expiry is not always pushed to
        clients at all. Asking Telegram which cached ids still exist is the
        only way to notice — the cache is the sole record that they existed.
        """
        if self.watch_ids:
            chat_ids = sorted(self.watch_ids)
        else:
            chat_ids = self.storage.recent_chats(since_ts)
        total = 0
        for chat_id in chat_ids:
            if not self.is_tracked(chat_id):
                continue
            if total >= RECONCILE_MAX_REPORTS:
                self.log.warning(
                    "reconcile: report budget spent, remaining deletions wait "
                    "for the next pass"
                )
                break
            total += await self.reconcile_chat(
                chat_id, since_ts, RECONCILE_MAX_REPORTS - total
            )
        return total

    async def reconcile_chat(self, chat_id: int, since_ts: int, budget: int) -> int:
        reported = 0
        ids = self.storage.alive_ids(chat_id, since_ts)
        reconciled_deleted: set[int] = set()
        for start in range(0, len(ids), RECONCILE_CHUNK):
            if reported >= budget:
                break
            chunk = ids[start : start + RECONCILE_CHUNK]
            try:
                fetched = await self.client.get_messages(chat_id, ids=chunk)
            except Exception:
                self.log.exception(
                    "reconcile: cannot fetch messages for chat [%s]",
                    self.chat_label(chat_id),
                )
                break
            for msg_id, message in zip(chunk, fetched):
                if message is not None:
                    continue  # still there
                stored = self.storage.find_message(chat_id, msg_id)
                if stored is None or stored.deleted:
                    continue
                if stored.reply_to_msg_id:
                    parent = self.storage.find_message(chat_id, stored.reply_to_msg_id)
                    if (
                        parent is not None
                        and parent.sender_id != stored.sender_id
                        and (
                            parent.msg_id in reconciled_deleted
                            or self.is_recently_deleted(chat_id, parent.msg_id)
                        )
                    ):
                        self.storage.mark_deleted(stored.chat_id, stored.msg_id)
                        self.record_deletion(stored.chat_id, stored.msg_id)
                        reconciled_deleted.add(stored.msg_id)
                        continue
                if reported >= budget:
                    break
                if await self.report_deleted(stored, source="reconcile"):
                    reported += 1
                    reconciled_deleted.add(stored.msg_id)
                await asyncio.sleep(RECONCILE_PAUSE_SEC)
            await asyncio.sleep(RECONCILE_PAUSE_SEC)
        return reported
