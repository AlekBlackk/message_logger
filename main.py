"""Entry point: build the client, wire handlers, run until stopped."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from telethon import TelegramClient, utils

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


async def run(cfg: Config) -> None:
    storage = Storage(cfg.db_path)
    client = build_client(cfg)
    await client.start()
    log.info("connected as %s", utils.get_display_name(await client.get_me()))

    log_target_id = utils.get_peer_id(await client.get_entity(cfg.log_target))
    watch_ids = await resolve_peer_ids(client, cfg.watch_chats)
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
    try:
        # Poll instead of a bare stop.wait(): on Windows the loop must wake up
        # regularly for a Ctrl+C signal handler to run at all.
        while not stop.is_set() and client.is_connected():
            await asyncio.sleep(1)
    finally:
        retention_task.cancel()
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
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
