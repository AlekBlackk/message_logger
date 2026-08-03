"""Rendering of deletion and edit reports for Telegram (parse_mode='html')."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from storage import StoredMessage

MAX_TEXT_LEN = 3500


def truncate(text: str | None, limit: int = MAX_TEXT_LEN) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _esc(value: str | None, fallback: str = "?") -> str:
    return html.escape(value) if value else fallback


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _author_line(msg: StoredMessage, sender_username: str | None) -> str:
    name = _esc(msg.sender_name, "неизвестен")
    if sender_username:
        author = f'<a href="https://t.me/{html.escape(sender_username)}">{name}</a>'
    elif msg.sender_id is not None:
        author = f'<a href="tg://user?id={msg.sender_id}">{name}</a>'
    else:
        author = name
    return (
        f"<b>Автор:</b> {author} "
        f"(<code>{msg.sender_id if msg.sender_id is not None else '?'}</code>)"
    )


def _header(
    msg: StoredMessage,
    title: str,
    username: str | None = None,
    sender_username: str | None = None,
) -> list[str]:
    chat_line = f"<b>Чат:</b> {_esc(msg.chat_title, 'без названия')}"
    if username:
        chat_line += f" (@{html.escape(username)})"
    chat_line += f" (<code>{msg.chat_id}</code>)"
    return [
        title,
        chat_line,
        _author_line(msg, sender_username),
        f"<b>Отправлено:</b> {_when(msg.date)}",
        f"<b>ID сообщения:</b> <code>{msg.msg_id}</code>",
    ]


def _body(
    text: str | None, media_type: str | None, limit: int = MAX_TEXT_LEN
) -> str:
    escaped = html.escape(truncate(text, limit))
    if escaped:
        return escaped
    if media_type:
        return f"<i>без текста, вложение: {html.escape(media_type)}</i>"
    return "<i>без текста</i>"


def format_deleted(
    msg: StoredMessage,
    username: str | None = None,
    sender_username: str | None = None,
) -> str:
    lines = _header(msg, "🗑 <b>Удалённое сообщение</b>", username, sender_username)
    lines.append("")
    lines.append(_body(msg.text, msg.media_type))
    return "\n".join(lines)


def format_edited(
    msg: StoredMessage,
    old_text: str | None,
    new_text: str | None,
    username: str | None = None,
    sender_username: str | None = None,
) -> str:
    # Two bodies in one message — halve the budget so the report stays under
    # Telegram's 4096-character ceiling.
    half = MAX_TEXT_LEN // 2
    lines = _header(msg, "✏️ <b>Сообщение отредактировано</b>", username, sender_username)
    lines.append("")
    lines.append("<b>Было:</b>")
    lines.append(_body(old_text, msg.media_type, half))
    lines.append("")
    lines.append("<b>Стало:</b>")
    lines.append(_body(new_text, msg.media_type, half))
    return "\n".join(lines)
