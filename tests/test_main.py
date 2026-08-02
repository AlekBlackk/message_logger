import asyncio
import types

import pytest

import main
from tests.test_handlers_new import make_config

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


async def test_log_target_is_added_to_ignore(tmp_path, monkeypatch):
    """Point 4 of the spec: reports must never be cached back."""
    cfg = make_config(tmp_path, ignore_chats=("@x",))
    client = ResolvingClient({"@x": entity(5), "me": entity(777)})
    monkeypatch.setattr(main.utils, "get_peer_id", lambda e: e.id)
    ignore = await main.resolve_peer_ids(client, cfg.ignore_chats)
    target = main.utils.get_peer_id(await client.get_entity(cfg.log_target))
    ignore.add(target)
    assert ignore == {5, 777}


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
