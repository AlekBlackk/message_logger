import asyncio
import types

import pytest

import main
from tests.test_handlers_new import make_config, make_logger

pytestmark = pytest.mark.asyncio


class ResolvingClient:
    def __init__(self, mapping, fail_on=()):
        self.mapping = mapping
        self.fail_on = set(fail_on)
        self.asked = []

    async def get_entity(self, ref):
        self.asked.append(ref)
        if ref in self.fail_on:
            raise ValueError(f"cannot find {ref}")
        return self.mapping[ref]


def entity(raw_id, is_channel=False):
    if is_channel:
        return types.SimpleNamespace(id=raw_id, title="chan", broadcast=True,
                                     megagroup=False, access_hash=1)
    return types.SimpleNamespace(id=raw_id, first_name="u", last_name=None,
                                 access_hash=1)


async def test_resolve_empty_list():
    client = ResolvingClient({})
    assert await main.resolve_peer_ids(client, ()) == set()


async def test_resolve_numeric_and_username(monkeypatch):
    """Numeric refs must reach get_entity as ints — Telethon does not resolve a
    numeric string as a username (utils.parse_username('-1001222') is None)."""
    client = ResolvingClient({"@a": entity(111), -1001222: entity(222, True)})
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id * 10)
    got = await main.resolve_peer_ids(client, ("@a", "-1001222"))
    assert got == {1110, 2220}
    assert client.asked == ["@a", -1001222]


async def test_resolve_skips_unresolvable(monkeypatch, caplog):
    client = ResolvingClient({"@a": entity(111)}, fail_on=("@bad",))
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    got = await main.resolve_peer_ids(client, ("@a", "@bad"))
    assert got == {111}


class FakeRunClient:
    """Minimal stand-in for TelegramClient's run() surface."""

    def __init__(self, entities, *, start_error=None):
        self.entities = entities
        self.start_error = start_error
        self.started = False
        self.disconnect_calls = 0
        self.handlers = []
        # Already-resolved: run()'s loop exits on the first check, no sleeping.
        self.disconnected = asyncio.get_running_loop().create_future()
        self.disconnected.set_result(None)

    async def start(self):
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    async def get_me(self):
        return entity(999)

    async def get_entity(self, ref):
        return self.entities[ref]

    def add_event_handler(self, *_a, **_kw):
        self.handlers.append(_a)

    async def disconnect(self):
        self.disconnect_calls += 1


async def test_run_wires_log_target_into_ignore_set(tmp_path, monkeypatch):
    """run() must force the log target into ignore_ids — the feedback-loop guard."""
    cfg = make_config(tmp_path, log_target="me", ignore_chats=("@x",))
    client = FakeRunClient({"me": entity(777), "@x": entity(5)})
    monkeypatch.setattr(main, "build_client", lambda _cfg: client)
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    monkeypatch.setattr(main.utils, "get_display_name", lambda e: "me")

    captured = {}

    class RecordingLogger:
        def __init__(self, **kw):
            captured.update(kw)

        def register(self):
            captured["registered"] = True

    monkeypatch.setattr(main, "MessageLogger", RecordingLogger)

    await main.run(cfg)

    assert captured["log_target_id"] == 777
    assert 777 in captured["ignore_ids"]
    assert 5 in captured["ignore_ids"]
    assert captured["registered"] is True
    assert client.disconnect_calls == 1


async def test_run_closes_storage_when_startup_fails(tmp_path, monkeypatch):
    """A failure after Storage() is constructed must still release the handle."""
    cfg = make_config(tmp_path)
    client = FakeRunClient({}, start_error=RuntimeError("no network"))
    monkeypatch.setattr(main, "build_client", lambda _cfg: client)

    closed = []
    real_close = main.Storage.close

    def tracking_close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(main.Storage, "close", tracking_close)

    with pytest.raises(RuntimeError, match="no network"):
        await main.run(cfg)

    assert closed, "storage.close() must run even when startup fails"
    assert client.disconnect_calls == 1


async def test_run_closes_storage_when_build_client_fails(tmp_path, monkeypatch):
    """storage must not leak even if build_client() itself raises, before any
    client exists to disconnect."""
    cfg = make_config(tmp_path)

    def failing_build_client(_cfg):
        raise RuntimeError("bad session file")

    monkeypatch.setattr(main, "build_client", failing_build_client)

    closed = []
    real_close = main.Storage.close

    def tracking_close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(main.Storage, "close", tracking_close)

    with pytest.raises(RuntimeError, match="bad session file"):
        await main.run(cfg)

    assert closed, "storage.close() must run even when build_client() fails"


async def test_run_calls_backfill_after_register(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, backfill_on_start=True, backfill_limit=5,
                       watch_chats=("@a",))
    client = FakeRunClient({"me": entity(777), "@a": entity(-1001)})
    monkeypatch.setattr(main, "build_client", lambda _cfg: client)
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    monkeypatch.setattr(main.utils, "get_display_name", lambda e: "name")

    call_order = []

    class RecordingLogger:
        def __init__(self, **_kw):
            pass

        def register(self):
            call_order.append("register")

        def chat_title(self, _peer_id):
            return None

        def seed_chat_title(self, _peer_id, _title):
            pass

    monkeypatch.setattr(main, "MessageLogger", RecordingLogger)

    async def fake_backfill(_client, _logger, _cfg, _watch_ids):
        call_order.append("backfill")
        return 0

    monkeypatch.setattr(main, "backfill", fake_backfill)

    await main.run(cfg)

    assert call_order == ["register", "backfill"]


async def test_load_watch_ids_migrates_env_list_into_empty_db(tmp_path, monkeypatch):
    _logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, watch_chats=("@a",))
    client = ResolvingClient({"@a": entity(111)})
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    ids = await main.load_watch_ids(client, db, cfg)
    assert ids == {111}
    rows = db.list_watch()
    assert len(rows) == 1
    assert rows[0][0] == 111
    db.close()


async def test_load_watch_ids_prefers_existing_db_rows_over_env(tmp_path, monkeypatch):
    _logger, _c, db = make_logger(tmp_path)
    db.add_watch(222, "already there", 500)
    cfg = make_config(tmp_path, watch_chats=("@a",))
    client = ResolvingClient({"@a": entity(111)})
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    ids = await main.load_watch_ids(client, db, cfg)
    assert ids == {222}
    assert client.asked == []
    db.close()


async def test_load_watch_ids_empty_when_no_env_list_and_empty_db(tmp_path):
    _logger, _c, db = make_logger(tmp_path)
    cfg = make_config(tmp_path, watch_chats=())
    client = ResolvingClient({})
    ids = await main.load_watch_ids(client, db, cfg)
    assert ids == set()
    db.close()
