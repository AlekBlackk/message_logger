"""Entry point: build the client, wire handlers, run until stopped."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.tl.types import Channel

from commands import CommandHandler
from config import Config, ConfigError, load_account_configs
from handlers import MessageLogger
from storage import Storage

log = logging.getLogger("message_log")

RETENTION_INTERVAL_SEC = 2 * 86400


def setup_logging(cfg: Config) -> None:
    """Logging is process-wide, so with several accounts the first one's
    LOG_LEVEL/LOG_FILE wins — per-account log files would need separate
    handlers per logger, which nothing here asks for."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if cfg.log_file is not None:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                cfg.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
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
    # catch_up=True makes Telethon fetch the updates missed while the process
    # was down; without it every deletion during a restart is lost for good.
    return TelegramClient(
        cfg.session_name,
        cfg.api_id,
        cfg.api_hash,
        proxy=cfg.proxy_dict(),
        catch_up=True,
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


# The live update stream is the fast path; the first sweep waits so it does not
# compete with backfill and startup traffic for the same rate limit.
RECONCILE_STARTUP_DELAY_SEC = 60


async def reconcile_loop(cfg: Config, logger: MessageLogger) -> None:
    await asyncio.sleep(RECONCILE_STARTUP_DELAY_SEC)
    while True:
        try:
            since = int(time.time()) - cfg.reconcile_window_hours * 3600
            reported = await logger.reconcile(since)
            if reported:
                logger.log.info(
                    "reconcile: reported %s deletion(s) the update stream missed",
                    reported,
                )
        except Exception:
            logger.log.exception("reconcile pass failed")
        await asyncio.sleep(cfg.reconcile_interval_min * 60)


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


async def backfill(
    client,
    logger: MessageLogger,
    cfg: Config,
    watch_ids: set[int],
    stop: asyncio.Event,
) -> int:
    """Warm the cache with recent history for explicitly watched chats.

    Runs only against an explicit watch list — sweeping every dialog of an
    account with hundreds of chats invites FloodWait with no upper bound.
    Existing rows are never overwritten, so a deleted=1 flag survives a restart.
    Sends no reports: this is cache filling, not an event.

    stop is checked between chats so a shutdown requested mid-backfill (which
    can run for minutes across many chats) breaks out early instead of leaving
    Ctrl+C/SIGTERM unresponsive for the whole window.
    """
    if not cfg.backfill_on_start:
        return 0
    if not watch_ids:
        log.info("backfill skipped: watch list is empty (watching all chats)")
        return 0

    total = 0
    for peer_id in sorted(watch_ids):
        if stop.is_set():
            log.info("backfill: shutdown requested, stopping early")
            break
        cached = await backfill_one_chat(client, logger, cfg, peer_id)
        total += cached
        log.info(
            "backfill: cached %s messages from [%s]", cached, logger.chat_label(peer_id)
        )
        if stop.is_set():
            log.info("backfill: shutdown requested, stopping early")
            break
        await asyncio.sleep(BACKFILL_PAUSE_SEC)
    log.info("backfill done: %s messages", total)
    return total


async def run_account(label: str, cfg: Config, stop: asyncio.Event) -> bool:
    """Runs one account end-to-end. Never raises: an unhandled exception is
    logged and swallowed here so one account's failure can't cancel its
    siblings under the asyncio.gather() in run_all().

    Returns True if the account reached and completed the polling loop
    normally, False if it crashed before or during that — this lets main()
    tell a total failure (every account crashed) from a normal shutdown."""
    acc_log = logging.getLogger(f"message_log.{label}")
    try:
        storage = Storage(cfg.db_path)
    except Exception:
        # Nothing exists yet to close — log and bail rather than let this
        # escape run_all()'s asyncio.gather(), whose return list is unread.
        acc_log.exception("account %s crashed", label)
        return False
    client = None
    background: list[asyncio.Task] = []
    ok = False
    try:
        client = build_client(cfg)
        await client.start()
        acc_log.info("connected as %s", utils.get_display_name(await client.get_me()))

        log_target_id = utils.get_peer_id(await client.get_entity(cfg.log_target))
        watch_ids = await load_watch_ids(client, storage, cfg)
        ignore_ids = await resolve_peer_ids(client, cfg.ignore_chats)
        ignore_ids.add(log_target_id)  # never cache our own reports
        acc_log.info(
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
            log=acc_log,
        )
        logger.register()

        commands_handler = CommandHandler(
            client=client,
            storage=storage,
            logger=logger,
            config=cfg,
            log_target_id=log_target_id,
            backfill_fn=backfill_one_chat,
            account_label=label,
        )
        commands_handler.register()

        await backfill(client, logger, cfg, watch_ids, stop)

        background.append(asyncio.create_task(retention_loop(cfg, storage)))
        if cfg.reconcile_interval_min > 0:
            background.append(asyncio.create_task(reconcile_loop(cfg, logger)))
        # Poll rather than awaiting a single future: on Windows the loop must
        # wake up regularly for a Ctrl+C signal handler to run at all.
        # client.disconnected (not is_connected()) is the exit signal — it
        # resolves only once Telethon has given up reconnecting, so a transient
        # network drop no longer shuts the bot down.
        while not stop.is_set() and not client.disconnected.done():
            await asyncio.sleep(1)
        ok = True
    except Exception:
        acc_log.exception("account %s crashed", label)
    finally:
        # Each cleanup step is guarded independently so a failure in one
        # (e.g. a hung disconnect) can't skip the ones after it — and none of
        # them may let an exception escape run_account itself.
        for task in background:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                acc_log.exception("account %s: background task cleanup crashed", label)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                acc_log.exception("account %s: client disconnect crashed", label)
        try:
            storage.close()
        except Exception:
            acc_log.exception("account %s: storage close crashed", label)
        acc_log.info("stopped cleanly")
    return ok


async def run_all(accounts: list[tuple[str, Config]]) -> list:
    stop = asyncio.Event()

    def request_stop(*_args) -> None:
        log.info("shutdown requested")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError, AttributeError):
            pass

    return await asyncio.gather(
        *(run_account(label, cfg, stop) for label, cfg in accounts),
        return_exceptions=True,
    )


def main() -> None:
    try:
        accounts = load_account_configs()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    setup_logging(accounts[0][1])
    results = asyncio.run(run_all(accounts))
    if not any(result is True for result in results):
        log.error("all accounts failed to start; exiting with non-zero status")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
