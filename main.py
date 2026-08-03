"""Entry point: build the client, wire handlers, run until stopped."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.tl.types import Channel

from commands import CommandHandler
from config import Config, ConfigError, load_config
from handlers import MessageLogger
from storage import Storage

log = logging.getLogger("message_log")

RETENTION_INTERVAL_SEC = 3600


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def resolve_peer_ids(client, refs: tuple[str, ...]) -> set[int]:
    """Turn .env chat references into normalized peer ids.

    utils.get_peer_id is mandatory here: supergroup updates carry -100xxxxxxxxxx
    while get_entity().id returns the raw id, so a naive compare never matches.
    """
    resolved: set[int] = set()
    for ref in refs:
        try:
            entity = await client.get_entity(int(ref) if _is_int(ref) else ref)
            resolved.add(utils.get_peer_id(entity))
        except Exception as exc:
            log.warning("cannot resolve chat %r: %s", ref, exc)
    return resolved


def _is_int(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True


async def load_watch_ids(client, storage: Storage, cfg: Config) -> set[int]:
    """The watch_chats table is the source of truth; WATCH_CHATS in .env is
    only consulted once, to seed the table when the database is still empty."""
    existing = storage.list_watch()
    if existing:
        return {chat_id for chat_id, _label, _added_at in existing}
    if not cfg.watch_chats:
        return set()
    resolved = await resolve_peer_ids(client, cfg.watch_chats)
    now = int(time.time())
    for peer_id in resolved:
        storage.add_watch(peer_id, None, now)
    log.info("migrated %s chat(s) from WATCH_CHATS into the database", len(resolved))
    return resolved


def build_client(cfg: Config) -> TelegramClient:
    return TelegramClient(
        cfg.session_name,
        cfg.api_id,
        cfg.api_hash,
        proxy=cfg.proxy_dict(),
    )


async def retention_loop(cfg: Config, storage: Storage) -> None:
    while True:
        try:
            cutoff = int(time.time()) - cfg.retention_days * 86400
            paths = storage.purge_older_than(cutoff)
            for path in paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("cannot delete media %s: %s", path, exc)
            if paths:
                log.info("retention: purged %s media files", len(paths))
        except Exception:
            log.exception("retention pass failed")
        await asyncio.sleep(RETENTION_INTERVAL_SEC)


BACKFILL_PAUSE_SEC = 1
BACKFILL_MEDIA_PAUSE_SEC = 0.5


async def backfill_one_chat(client, logger: MessageLogger, cfg: Config, peer_id: int) -> int:
    """Cache one chat's recent history. Never raises — logs and returns the
    partial count on failure, so a bad chat can't abort the whole run."""
    cached = 0
    try:
        entity = await client.get_entity(peer_id)
        is_channel = isinstance(entity, Channel)
        chat_title = utils.get_display_name(entity) or logger.chat_title(peer_id)
        logger.seed_chat_title(peer_id, chat_title)
        logger.seed_chat_username(peer_id, getattr(entity, "username", None))
        async for message in client.iter_messages(peer_id, limit=cfg.backfill_limit):
            await logger.store_message(
                message,
                peer_id=peer_id,
                is_channel=is_channel,
                chat_title=chat_title,
                overwrite=False,
            )
            cached += 1
            if message.media is not None:
                await asyncio.sleep(BACKFILL_MEDIA_PAUSE_SEC)
    except Exception:
        log.exception("backfill failed for chat %s", peer_id)
    return cached


async def backfill(client, logger: MessageLogger, cfg: Config, watch_ids: set[int]) -> int:
    """Warm the cache with recent history for explicitly watched chats.

    Runs only against an explicit watch list — sweeping every dialog of an
    account with hundreds of chats invites FloodWait with no upper bound.
    Existing rows are never overwritten, so a deleted=1 flag survives a restart.
    Sends no reports: this is cache filling, not an event.
    """
    if not cfg.backfill_on_start:
        return 0
    if not watch_ids:
        log.info("backfill skipped: watch list is empty (watching all chats)")
        return 0

    total = 0
    for peer_id in sorted(watch_ids):
        cached = await backfill_one_chat(client, logger, cfg, peer_id)
        total += cached
        log.info(
            "backfill: cached %s messages from [%s]", cached, logger.chat_label(peer_id)
        )
        await asyncio.sleep(BACKFILL_PAUSE_SEC)
    log.info("backfill done: %s messages", total)
    return total


async def run(cfg: Config) -> None:
    storage = Storage(cfg.db_path)
    client = None
    retention_task: asyncio.Task | None = None
    try:
        client = build_client(cfg)
        await client.start()
        log.info("connected as %s", utils.get_display_name(await client.get_me()))

        log_target_id = utils.get_peer_id(await client.get_entity(cfg.log_target))
        watch_ids = await load_watch_ids(client, storage, cfg)
        ignore_ids = await resolve_peer_ids(client, cfg.ignore_chats)
        ignore_ids.add(log_target_id)  # never cache our own reports
        log.info(
            "log target %s, watching %s chats, ignoring %s chats",
            log_target_id,
            len(watch_ids) or "all",
            len(ignore_ids),
        )

        logger = MessageLogger(
            client=client,
            storage=storage,
            config=cfg,
            log_target_id=log_target_id,
            watch_ids=watch_ids,
            ignore_ids=ignore_ids,
        )
        logger.register()

        commands_handler = CommandHandler(
            client=client,
            storage=storage,
            logger=logger,
            config=cfg,
            log_target_id=log_target_id,
            backfill_fn=backfill_one_chat,
        )
        commands_handler.register()

        await backfill(client, logger, cfg, watch_ids)

        stop = asyncio.Event()

        def request_stop(*_args) -> None:
            log.info("shutdown requested")
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError, AttributeError):
                pass

        retention_task = asyncio.create_task(retention_loop(cfg, storage))
        # Poll rather than awaiting a single future: on Windows the loop must
        # wake up regularly for a Ctrl+C signal handler to run at all.
        # client.disconnected (not is_connected()) is the exit signal — it
        # resolves only once Telethon has given up reconnecting, so a transient
        # network drop no longer shuts the bot down.
        while not stop.is_set() and not client.disconnected.done():
            await asyncio.sleep(1)
    finally:
        if retention_task is not None:
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass
        if client is not None:
            await client.disconnect()
        storage.close()
        log.info("stopped cleanly")


def main() -> None:
    setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
