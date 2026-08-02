import datetime
import types

import pytest
from telethon.tl.types import Chat

import main
from tests.test_handlers_new import make_config, make_logger

pytestmark = pytest.mark.asyncio


def fake_message(msg_id, text="old text", sender_id=42):
    return types.SimpleNamespace(
        id=msg_id,
        message=text,
        text=text,
        media=None,
        file=None,
        from_id=None,
        sender_id=sender_id,
        date=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        out=False,
    )


class HistoryClient:
    def __init__(self, per_chat, entities=None):
        self.per_chat = per_chat
        self.entities = entities or {}
        self.calls = []

    async def get_entity(self, ref):
        if ref in self.entities:
            return self.entities[ref]
        return types.SimpleNamespace(id=ref, title="t")

    def iter_messages(self, peer, limit=None):
        self.calls.append((peer, limit))
        messages = self.per_chat.get(peer, [])

        async def gen():
            for m in messages[:limit]:
                yield m

        return gen()


async def test_backfill_caches_history(tmp_path, monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@a",))
    client = HistoryClient({-1001: [fake_message(1), fake_message(2)]})
    count = await main.backfill(client, logger, cfg, {-1001})
    assert count == 2
    assert db.find_message(-1001, 1) is not None
    db.close()


async def test_backfill_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=1,
                      watch_chats=("@a",))
    client = HistoryClient({-1001: [fake_message(1), fake_message(2)]})
    await main.backfill(client, logger, cfg, {-1001})
    assert client.calls == [(-1001, 1)]
    db.close()


async def test_backfill_skipped_when_disabled(tmp_path):
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=False, watch_chats=("@a",))
    client = HistoryClient({-1001: [fake_message(1)]})
    assert await main.backfill(client, logger, cfg, {-1001}) == 0
    assert client.calls == []
    db.close()


async def test_backfill_skipped_without_watch_list(tmp_path):
    """Crawling every dialog of an arbitrary account is a FloodWait trap."""
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, watch_chats=())
    client = HistoryClient({-1001: [fake_message(1)]})
    assert await main.backfill(client, logger, cfg, set()) == 0
    assert client.calls == []
    db.close()


async def test_backfill_does_not_overwrite_deleted_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@a",))
    client = HistoryClient({-1001: [fake_message(1, text="from history")]})
    await main.backfill(client, logger, cfg, {-1001})
    db.mark_deleted(-1001, 1)
    await main.backfill(client, logger, cfg, {-1001})
    row = db.find_message(-1001, 1)
    assert row.deleted is True
    db.close()


async def test_backfill_sends_no_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, client_fake, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@a",))
    client = HistoryClient({-1001: [fake_message(1)]})
    await main.backfill(client, logger, cfg, {-1001})
    assert client_fake.sent == []
    assert client_fake.files == []
    db.close()


async def test_backfill_derives_is_channel_from_entity_type_not_id_shape(tmp_path, monkeypatch):
    """A basic-group peer id can coincidentally look channel-shaped as a string
    (e.g. raw group id 100 -> marked id -100, which starts with "-100" just like
    a real channel id does) — is_channel must come from the resolved entity's
    real type, not a string-prefix guess on the numeric id."""
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@g",))
    group_peer_id = -100  # collides with the old string-prefix heuristic
    client = HistoryClient(
        {group_peer_id: [fake_message(1)]},
        entities={
            group_peer_id: Chat(
                id=100, title="Group", photo=None,
                participants_count=0, date=None, version=0,
            )
        },
    )
    await main.backfill(client, logger, cfg, {group_peer_id})
    stored = db.find_message(None, 1)
    assert stored is not None
    assert stored.is_channel is False
    db.close()


async def _no_sleep(_seconds):
    return None
