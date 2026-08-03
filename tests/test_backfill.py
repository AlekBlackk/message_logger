import datetime
import types

import pytest
from telethon.tl.types import Chat

import main
from tests.test_handlers_new import make_config, make_logger

pytestmark = pytest.mark.asyncio


def fake_message(msg_id, text="old text", sender_id=42, media=None, file=None):
    return types.SimpleNamespace(
        id=msg_id,
        message=text,
        text=text,
        media=media,
        file=file,
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


async def test_backfill_populates_chat_title_from_entity(tmp_path, monkeypatch):
    """entity is already fetched for is_channel — its display name must be used
    for chat_title too, instead of the (empty at startup) in-memory titles cache.

    NOTE: HistoryClient's bare SimpleNamespace fallback (`title="t"` but not a
    real Chat/Channel/User) does not work here — telethon.utils.get_display_name
    dispatches on isinstance(entity, (Chat, Channel, ...)), not duck-typing on a
    `.title` attribute, so a SimpleNamespace always yields ''. A real Chat entity
    is supplied instead, matching the pattern already used by
    test_backfill_derives_is_channel_from_entity_type_not_id_shape below.
    """
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@a",))
    client = HistoryClient(
        {-1001: [fake_message(1)]},
        entities={-1001: Chat(id=1001, title="t", photo=None,
                               participants_count=0, date=None, version=0)},
    )
    await main.backfill(client, logger, cfg, {-1001})
    stored = db.find_message(-1001, 1)
    assert stored.chat_title == "t"
    db.close()


async def test_backfill_pauses_after_media_messages(tmp_path, monkeypatch):
    sleep_calls = []

    async def recording_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", recording_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=10,
                      watch_chats=("@a",), max_media_mb=1)
    media_file = types.SimpleNamespace(size=10, ext=".jpg", mime_type="image/jpeg")
    client = HistoryClient({
        -1001: [
            fake_message(1, media=object(), file=media_file),
            fake_message(2),  # text-only, no media
        ]
    })
    await main.backfill(client, logger, cfg, {-1001})
    # One media-message pause + one between-chat pause = two sleep calls total
    assert sleep_calls.count(main.BACKFILL_MEDIA_PAUSE_SEC) == 1
    db.close()


async def _no_sleep(_seconds):
    return None


async def test_backfill_one_chat_caches_and_returns_count(tmp_path, monkeypatch):
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_limit=10)
    client = HistoryClient({-1001: [fake_message(1), fake_message(2)]})
    count = await main.backfill_one_chat(client, logger, cfg, -1001)
    assert count == 2
    assert db.find_message(-1001, 1) is not None
    db.close()


async def test_backfill_one_chat_returns_zero_on_error(tmp_path):
    logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, backfill_limit=10)

    class FailingClient:
        async def get_entity(self, _ref):
            raise ValueError("gone")

    count = await main.backfill_one_chat(FailingClient(), logger, cfg, -1001)
    assert count == 0
    db.close()
