"""Rendering of deletion and edit reports for Telegram (parse_mode='html')."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from storage import StoredMessage

MAX_TEXT_LEN = 3500
MSK = timezone(timedelta(hours=3))


def truncate(text: str | None, limit: int = MAX_TEXT_LEN) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _esc(value: str | None, fallback: str = "?") -> str:
    return html.escape(value) if value else fallback


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=MSK).strftime("%Y-%m-%d %H:%M:%S MSK")


def _mention(name: str | None, entity_id: int | None, username: str | None) -> str:
    esc_name = _esc(name, "неизвестен")
    if username:
        return f'<a href="https://t.me/{html.escape(username)}">{esc_name}</a>'
    if entity_id is not None:
        return f'<a href="tg://user?id={entity_id}">{esc_name}</a>'
    return esc_name


def _who_line(label: str, name: str | None, entity_id: int | None, username: str | None) -> str:
    mention = _mention(name, entity_id, username)
    return f"<b>{label}:</b> {mention} (<code>{entity_id if entity_id is not None else '?'}</code>)"


def _author_line(msg: StoredMessage, sender_username: str | None) -> str:
    return _who_line("Автор", msg.sender_name, msg.sender_id, sender_username)


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


def format_membership(
    *,
    joined: bool,
    chat_title: str | None,
    chat_id: int,
    chat_username: str | None,
    user_name: str | None,
    user_id: int | None,
    user_username: str | None,
    actor_name: str | None = None,
    actor_id: int | None = None,
    actor_username: str | None = None,
) -> str:
    title = "➕ <b>Вступление в чат</b>" if joined else "➖ <b>Выход из чата</b>"
    chat_line = f"<b>Чат:</b> {_esc(chat_title, 'без названия')}"
    if chat_username:
        chat_line += f" (@{html.escape(chat_username)})"
    chat_line += f" (<code>{chat_id}</code>)"
    lines = [title, chat_line, _who_line("Пользователь", user_name, user_id, user_username)]
    if actor_name is not None or actor_id is not None:
        label = "Добавил" if joined else "Удалил"
        lines.append(_who_line(label, actor_name, actor_id, actor_username))
    return "\n".join(lines)
