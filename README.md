# message_log

Telethon userbot that caches incoming Telegram messages and reports deletions and
edits to Saved Messages, with the original media attached.

Personal account, personal use. Requires MTProto — the Bot API has no deletion event.

## Setup

Requires Python 3.11+.

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill `API_ID` / `API_HASH` from https://my.telegram.org
3. `python main.py` — Telethon asks for your phone, the login code and, if enabled,
   your 2FA password. A `<SESSION_NAME>.session` file is created.

## Configuration

| Variable | Meaning |
| --- | --- |
| `API_ID`, `API_HASH` | Credentials from my.telegram.org. Required. |
| `SESSION_NAME` | Session file name. Default `logger`. |
| `WATCH_CHATS` | Comma-separated ids or `@usernames`. Empty = every chat. |
| `IGNORE_CHATS` | Chats to skip. `LOG_TARGET` is always added automatically. |
| `LOG_TARGET` | Where reports go. `me` = Saved Messages. |
| `LOG_OUTGOING` | Also log your own deleted messages. |
| `LOG_EDITS` | Send a report when a message is edited. |
| `DOWNLOAD_MEDIA`, `MAX_MEDIA_MB`, `MEDIA_DIR` | Media capture settings. |
| `DB_PATH`, `RETENTION_DAYS` | Cache location and how long rows live. |
| `BACKFILL_ON_START`, `BACKFILL_LIMIT` | Warm the cache at startup. Needs a non-empty `WATCH_CHATS`. |
| `PROXY_ENABLED`, `PROXY_TYPE`, `PROXY_HOST`, `PROXY_PORT` | SOCKS/HTTP proxy. |

Values in `.env` take precedence over any same-named variable already set in your
shell environment.

## How it works

Every incoming message is written to SQLite along with its media, because after a
deletion the file reference on Telegram's servers is dead. When a deletion event
arrives it carries only message ids — and a chat id only for channels and
supergroups — so the cache is what makes a useful report possible at all.

## Limits

- If the bot is offline when a message is deleted, no report is produced. The cached
  copy stays, but the event is gone.
- Backfill only runs for an explicit `WATCH_CHATS` list.
- `RETENTION_DAYS` also deletes the cached media files for purged rows, not just the
  database rows.
- Message edit history is recorded in the database (`edits` table) but not currently
  surfaced in deletion/edit reports — `format_deleted` only shows the most recent
  text. This is intentional groundwork for a future history command, not a bug.

## Security

The `.session` file is full access to your account. Never commit it, never share it.
`.gitignore` already covers `*.session`, `*.db*`, `.env` and `media/`.

## Tests

`pip install -r requirements-dev.txt && python -m pytest`
