"""CLI viewer for deleted messages stored in messages.db."""

from __future__ import annotations

import argparse
import datetime
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Просмотр удалённых сообщений из базы данных.")
    parser.add_argument("-db", "--database", default="messages.db", help="Путь к файлу БД (по умолчанию messages.db)")
    parser.add_argument("-n", "--limit", type=int, default=20, help="Количество выводимых сообщений (по умолчанию 20, 0 = все)")
    parser.add_argument("-s", "--search", type=str, default="", help="Поиск по тексту сообщения, имени или чату")
    args = parser.parse_args()

    db_path = Path(args.database)
    if not db_path.exists():
        print(f"Ошибка: файл базы данных '{db_path}' не найден в текущей папке.")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    query = """
        SELECT date, chat_title, sender_name, text, media_type, media_path
        FROM messages
        WHERE deleted = 1
    """
    params = []

    if args.search:
        query += " AND (text LIKE ? OR chat_title LIKE ? OR sender_name LIKE ?)"
        s = f"%{args.search}%"
        params.extend([s, s, s])

    query += " ORDER BY date DESC"

    if args.limit > 0:
        query += f" LIMIT {args.limit}"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("Удалённых сообщений не найдено.")
        return

    print("=" * 70)
    print(f"Найдено удалённых сообщений: {len(rows)}")
    print("=" * 70)

    for r in rows:
        ts, chat_title, sender_name, text, media_type, media_path = r
        dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        chat = chat_title or "—"
        sender = sender_name or "—"
        body = text if text else "(без текста)"
        media = media_type or "—"

        print(f"[{dt}]  Чат: {chat}  |  От: {sender}")
        print(f"Текст: {body}")
        if media_type or media_path:
            print(f"Медиа: {media} {f'({media_path})' if media_path else ''}")
        print("-" * 70)


if __name__ == "__main__":
    main()
