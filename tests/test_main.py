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
