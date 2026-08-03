import asyncio
import logging
import types

import pytest

import main
from tests.test_handlers_new import make_config, make_logger

# No module-level pytestmark: asyncio_mode = auto (pytest.ini) already
# collects async def tests automatically, and this file also has plain sync
# tests (main() calls asyncio.run() itself, so they must stay sync) that a
# blanket pytest.mark.asyncio would otherwise warn about.


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


async def test_run_account_wires_log_target_into_ignore_set(tmp_path, monkeypatch):
    """run_account() must force the log target into ignore_ids — the feedback-loop guard."""
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

    result = await main.run_account("test", cfg, asyncio.Event())

    assert captured["log_target_id"] == 777
    assert 777 in captured["ignore_ids"]
    assert 5 in captured["ignore_ids"]
    assert captured["registered"] is True
    assert client.disconnect_calls == 1
    assert result is True


async def test_run_account_closes_storage_when_startup_fails(tmp_path, monkeypatch, caplog):
    """A failure after Storage() is constructed must still release the handle,
    and must be logged rather than propagated — one account's crash can't be
    allowed to cancel its siblings under asyncio.gather()."""
    cfg = make_config(tmp_path)
    client = FakeRunClient({}, start_error=RuntimeError("no network"))
    monkeypatch.setattr(main, "build_client", lambda _cfg: client)

    closed = []
    real_close = main.Storage.close

    def tracking_close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(main.Storage, "close", tracking_close)

    with caplog.at_level(logging.ERROR):
        result = await main.run_account("acc1", cfg, asyncio.Event())

    assert closed, "storage.close() must run even when startup fails"
    assert client.disconnect_calls == 1
    assert any("acc1" in r.message and "crashed" in r.message for r in caplog.records)
    assert result is False


async def test_run_account_closes_storage_when_build_client_fails(tmp_path, monkeypatch, caplog):
    """storage must not leak even if build_client() itself raises, before any
    client exists to disconnect. The exception is logged, not propagated."""
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

    with caplog.at_level(logging.ERROR):
        result = await main.run_account("acc1", cfg, asyncio.Event())

    assert closed, "storage.close() must run even when build_client() fails"
    assert any("crashed" in r.message for r in caplog.records)
    assert result is False


async def test_run_account_logs_when_storage_construction_fails(tmp_path, monkeypatch, caplog):
    """If Storage() itself raises, there is nothing to close yet — but the
    failure must still be logged rather than escaping run_account uncaught,
    since run_all() (a later task) discards asyncio.gather()'s return list."""
    cfg = make_config(tmp_path)

    class FailingStorage:
        def __init__(self, _db_path):
            raise RuntimeError("disk full")

    monkeypatch.setattr(main, "Storage", FailingStorage)

    with caplog.at_level(logging.ERROR):
        result = await main.run_account("acc1", cfg, asyncio.Event())

    assert any("acc1" in r.message and "crashed" in r.message for r in caplog.records)
    assert result is False


async def test_run_account_logs_when_cleanup_disconnect_fails(tmp_path, monkeypatch, caplog):
    """A failure inside the finally block's cleanup (client.disconnect() here)
    must be logged and swallowed, not propagated — and must not prevent the
    remaining cleanup steps (storage.close()) from still being attempted."""
    cfg = make_config(tmp_path)
    client = FakeRunClient({"me": entity(777)})

    async def failing_disconnect():
        client.disconnect_calls += 1
        raise RuntimeError("network gone")

    client.disconnect = failing_disconnect
    monkeypatch.setattr(main, "build_client", lambda _cfg: client)
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    monkeypatch.setattr(main.utils, "get_display_name", lambda e: "me")

    closed = []
    real_close = main.Storage.close

    def tracking_close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(main.Storage, "close", tracking_close)

    with caplog.at_level(logging.ERROR):
        result = await main.run_account("acc1", cfg, asyncio.Event())

    assert client.disconnect_calls == 1
    assert closed, "storage.close() must still run even though disconnect() raised"
    assert any("acc1" in r.message for r in caplog.records)
    # The crash happens in cleanup (finally), after the polling loop already
    # exited normally, so this still counts as a successful run.
    assert result is True


async def test_run_account_calls_backfill_after_register(tmp_path, monkeypatch):
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

    async def fake_backfill(_client, _logger, _cfg, _watch_ids, _stop):
        call_order.append("backfill")
        return 0

    monkeypatch.setattr(main, "backfill", fake_backfill)

    result = await main.run_account("test", cfg, asyncio.Event())

    assert call_order == ["register", "backfill"]
    assert result is True


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


async def test_run_all_isolates_account_failures(tmp_path, monkeypatch, caplog):
    """One account crashing must not stop asyncio.gather() from completing the
    others — this is the reason run_account() catches instead of raising."""
    ok_cfg = make_config(tmp_path, session_name="ok")
    bad_cfg = make_config(tmp_path, session_name="bad", db_path=tmp_path / "bad.db")

    ok_client = FakeRunClient({"me": entity(1)})
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    monkeypatch.setattr(main.utils, "get_display_name", lambda e: "me")

    def build_client_for(cfg):
        if cfg.session_name == "bad":
            raise RuntimeError("boom")
        return ok_client

    monkeypatch.setattr(main, "build_client", build_client_for)

    class RecordingLogger:
        def __init__(self, **_kw):
            pass

        def register(self):
            pass

    monkeypatch.setattr(main, "MessageLogger", RecordingLogger)
    # run_all() installs real signal.signal handlers; without this they'd leak
    # into the rest of the pytest session, referencing this test's dead stop
    # Event. No test here exercises signal delivery, so a no-op is safe.
    monkeypatch.setattr(main.signal, "signal", lambda *_a, **_kw: None)

    with caplog.at_level(logging.ERROR):
        results = await main.run_all([("ok", ok_cfg), ("bad", bad_cfg)])

    assert ok_client.disconnect_calls == 1
    assert any("bad" in r.message and "crashed" in r.message for r in caplog.records)
    assert results == [True, False]


def test_main_exits_nonzero_when_all_accounts_fail(monkeypatch):
    """A supervisor (systemd Restart=on-failure, a Docker healthcheck) needs a
    non-zero exit code to know every account failed to start — run_account()
    never raises, so main() must inspect run_all()'s results instead."""
    monkeypatch.setattr(main, "load_account_configs", lambda: [("a", object())])

    async def fake_run_all(_accounts):
        return [False]

    monkeypatch.setattr(main, "run_all", fake_run_all)

    with pytest.raises(SystemExit) as exc_info:
        main.main()
    assert exc_info.value.code == 1


def test_main_exits_nonzero_when_all_accounts_raise_via_gather(monkeypatch):
    """return_exceptions=True means a gather() result can be an exception
    object rather than False — that must count as failure too, defensively."""
    monkeypatch.setattr(main, "load_account_configs", lambda: [("a", object())])

    async def fake_run_all(_accounts):
        return [RuntimeError("boom")]

    monkeypatch.setattr(main, "run_all", fake_run_all)

    with pytest.raises(SystemExit) as exc_info:
        main.main()
    assert exc_info.value.code == 1


def test_main_does_not_exit_when_one_account_succeeds(monkeypatch):
    monkeypatch.setattr(main, "load_account_configs", lambda: [("a", object()), ("b", object())])

    async def fake_run_all(_accounts):
        return [False, True]

    monkeypatch.setattr(main, "run_all", fake_run_all)

    main.main()  # must not raise
