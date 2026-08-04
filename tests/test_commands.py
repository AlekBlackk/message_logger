import time
import types

import pytest
from telethon.tl.types import Chat

from commands import CommandHandler
from handlers import MessageLogger
from storage import Storage
from tests.test_handlers_new import make_config
from tests.test_storage import make_msg

pytestmark = pytest.mark.asyncio


def chat_entity(raw_id, title="Chat", username=None):
    entity = Chat(id=raw_id, title=title, photo=None, participants_count=0,
                  date=None, version=0)
    if username is not None:
        entity.username = username
    return entity


class FakeClient:
    def __init__(self, entities=None):
        self.entities = entities or {}
        self.sent = []
        self.files = []

    async def get_entity(self, ref):
        return self.entities[ref]

    async def send_message(self, target, text, **kw):
        self.sent.append((target, text, kw))

    async def send_file(self, target, file, **kw):
        self.files.append((target, file, kw))

    def add_event_handler(self, *_a, **_kw):
        pass


class FakeForward:
    def __init__(self, chat_id=None, sender_id=None):
        self.chat_id = chat_id
        self.sender_id = sender_id


class FakeReplyMessage:
    def __init__(self, forward=None):
        self.forward = forward


class FakeMessage:
    def __init__(self, text, reply=None):
        self.message = text
        self._reply = reply

    async def get_reply_message(self):
        return self._reply


class FakeEvent:
    def __init__(self, text, reply=None, out=True):
        self.message = FakeMessage(text, reply)
        self.out = out


def make_handler(tmp_path, *, entities=None, watch=None, account_label=""):
    client = FakeClient(entities=entities)
    db = Storage(tmp_path / "db.sqlite")
    cfg = make_config(tmp_path)
    logger = MessageLogger(
        client=client, storage=db, config=cfg, log_target_id=777,
        watch_ids=watch if watch is not None else set(), ignore_ids={777},
    )
    calls = []

    async def fake_backfill(_client, _logger, _cfg, peer_id):
        calls.append(peer_id)
        return 3

    handler = CommandHandler(
        client=client, storage=db, logger=logger, config=cfg,
        log_target_id=777, backfill_fn=fake_backfill,
        account_label=account_label,
    )
    return handler, client, db, calls


async def test_watch_by_username_arg_adds_and_backfills(tmp_path):
    handler, client, db, calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}
    )
    await handler.on_command(FakeEvent("/watch @a"))
    assert db.list_watch()[0][0] == -1001
    assert -1001 in handler.logger.watch_ids
    assert calls == [-1001]
    assert client.sent and "Chat A" in client.sent[-1][1]
    db.close()


async def test_watch_by_numeric_id_arg(tmp_path):
    handler, client, db, calls = make_handler(
        tmp_path, entities={-1001: chat_entity(1001, "Chat A")}
    )
    await handler.on_command(FakeEvent("/watch -1001"))
    assert db.list_watch()[0][0] == -1001
    db.close()


async def test_watch_by_reply_uses_forward_chat_id(tmp_path):
    handler, client, db, calls = make_handler(
        tmp_path, entities={-1001: chat_entity(1001, "Chat A")}
    )
    reply = FakeReplyMessage(forward=FakeForward(chat_id=-1001))
    await handler.on_command(FakeEvent("/watch", reply=reply))
    assert db.list_watch()[0][0] == -1001
    assert calls == [-1001]
    db.close()


async def test_watch_without_arg_or_forward_replies_with_error(tmp_path):
    handler, client, db, calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/watch", reply=FakeReplyMessage(forward=None)))
    assert db.list_watch() == []
    assert calls == []
    assert client.sent
    db.close()


async def test_watch_resolve_failure_replies_with_error_and_does_not_raise(tmp_path):
    handler, client, db, calls = make_handler(tmp_path, entities={})
    await handler.on_command(FakeEvent("/watch @missing"))
    assert db.list_watch() == []
    assert calls == []
    assert client.sent
    db.close()


async def test_unwatch_removes_from_db_and_live_set(tmp_path):
    handler, client, db, _calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}, watch={-1001}
    )
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/unwatch @a"))
    assert db.list_watch() == []
    assert -1001 not in handler.logger.watch_ids
    db.close()


async def test_unwatch_last_chat_notes_watching_everything(tmp_path):
    handler, client, db, _calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}, watch={-1001}
    )
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/unwatch @a"))
    assert "всеми" in client.sent[-1][1]
    db.close()


async def test_list_reports_empty(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/list"))
    assert "всеми" in client.sent[-1][1]
    db.close()


async def test_list_reports_watched_chats(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/list"))
    assert "Chat A" in client.sent[-1][1]
    assert "-1001" in client.sent[-1][1]
    db.close()


async def test_list_tags_rows_with_account_label(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path, account_label="acc1")
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/list"))
    assert "acc1" in client.sent[-1][1]
    db.close()


async def test_list_omits_tag_without_account_label(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/list"))
    assert "·" not in client.sent[-1][1]
    db.close()


async def test_incoming_command_from_other_sender_ignored(tmp_path):
    """A /watch sent by someone else in the log target (out=False) must not
    run — otherwise anyone in a non-"me" LOG_TARGET could control the bot."""
    handler, client, db, calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}
    )
    await handler.on_command(FakeEvent("/watch @a", out=False))
    assert db.list_watch() == []
    assert calls == []
    assert client.sent == []
    db.close()


async def test_list_paginates_when_over_message_limit(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    for i in range(400):
        db.add_watch(-1000 - i, f"Chat {i} " + "x" * 40, 1000 + i)
    await handler.on_command(FakeEvent("/list"))
    assert len(client.sent) > 1
    for target, text, _kw in client.sent:
        assert len(text) <= 3600
        assert target == 777
    assert "Chat 0 " in client.sent[0][1]
    assert "Chat 399 " in client.sent[-1][1]
    db.close()


async def test_non_command_message_ignored(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("напоминание себе"))
    assert client.sent == []
    assert db.list_watch() == []
    db.close()


async def test_unknown_slash_command_ignored(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/foo bar"))
    assert client.sent == []
    db.close()


async def test_purge_no_args_shows_usage(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/purge"))
    assert client.sent
    assert "confirm" in client.sent[-1][1]
    db.close()


async def test_purge_by_days_dry_run_reports_count(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100, media_path="media/1.jpg"))
    await handler.on_command(FakeEvent("/purge 30"))
    assert db.find_message(-1001, 1) is not None
    assert "1" in client.sent[-1][1]
    assert "confirm" in client.sent[-1][1]
    db.close()


async def test_purge_by_days_confirm_deletes(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge 30 confirm"))
    assert db.find_message(-1001, 1) is None
    assert "✅" in client.sent[-1][1]
    db.close()


async def test_purge_by_chat_dry_run(tmp_path):
    handler, client, db, _calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}
    )
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge @a"))
    assert db.find_message(-1001, 1) is not None
    assert db.find_message(-1002, 1) is not None
    assert "Chat A" in client.sent[-1][1]
    db.close()


async def test_purge_by_chat_confirm_deletes_only_that_chat(tmp_path):
    handler, client, db, _calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}
    )
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge @a confirm"))
    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1002, 1) is not None
    db.close()


async def test_purge_by_chat_and_days(tmp_path):
    """The `<days>` arg is converted to a real wall-clock cutoff (now - N days),
    so this needs dates relative to time.time() — a small fixed date like 100
    would always be "older" regardless of N and wouldn't exercise the filter."""
    handler, client, db, _calls = make_handler(
        tmp_path, entities={"@a": chat_entity(1001, "Chat A")}
    )
    now = int(time.time())
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=now - 10 * 86400))
    db.save_message(make_msg(chat_id=-1001, msg_id=2, date=now - 1 * 86400))
    await handler.on_command(FakeEvent("/purge @a 3 confirm"))
    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1001, 2) is not None
    db.close()


async def test_purge_zero_candidates_short_circuits(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/purge 30 confirm"))
    assert "Нечего" in client.sent[-1][1]
    db.close()


async def test_purge_rejects_non_positive_days(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/purge 0"))
    assert "⚠️" in client.sent[-1][1]
    db.close()


async def test_purge_rejects_too_many_args(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/purge @a 3 4 confirm"))
    assert "⚠️" in client.sent[-1][1]
    db.close()


async def test_purge_resolve_failure_replies_with_error_and_does_not_touch_db(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path, entities={})
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge @missing confirm"))
    assert db.find_message(-1001, 1) is not None
    assert "⚠️" in client.sent[-1][1]
    db.close()


async def test_purge_by_negative_numeric_id_resolves_as_chat(tmp_path):
    handler, client, db, _calls = make_handler(
        tmp_path, entities={-1001: chat_entity(1001, "Chat A")}
    )
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge -1001 confirm"))
    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1002, 1) is not None
    db.close()


async def test_purge_confirm_deletes_media_file_from_disk(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "1.jpg"
    media_file.write_bytes(b"fake-image-bytes")
    db.save_message(
        make_msg(chat_id=-1001, msg_id=1, date=100, media_path=str(media_file))
    )
    await handler.on_command(FakeEvent("/purge 30 confirm"))
    assert db.find_message(-1001, 1) is None
    assert not media_file.exists()
    assert "1" in client.sent[-1][1]
    db.close()


async def test_purge_confirm_missing_media_file_not_counted_as_removed(tmp_path):
    """media_path in DB but file already gone from disk (e.g. deleted
    outside purge) must not be reported as removed — unlink(missing_ok=True)
    silently no-ops on a missing file, so removed must track pre-unlink
    existence, not just absence of an exception."""
    handler, client, db, _calls = make_handler(tmp_path)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    ghost_path = media_dir / "gone.jpg"  # never created
    db.save_message(
        make_msg(chat_id=-1001, msg_id=1, date=100, media_path=str(ghost_path))
    )
    await handler.on_command(FakeEvent("/purge 30 confirm"))
    assert db.find_message(-1001, 1) is None
    report = client.sent[-1][1]
    assert "медиафайлов удалено 0 из 1" in report
    db.close()


async def test_purge_incoming_command_from_other_sender_ignored(tmp_path):
    """A /purge sent by someone else in the log target (out=False) must not
    run — a bypass here would mean irreversible data loss."""
    handler, client, db, _calls = make_handler(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    await handler.on_command(FakeEvent("/purge 1 confirm", out=False))
    assert db.find_message(-1001, 1) is not None
    assert client.sent == []
    db.close()


async def test_purge_huge_days_rejects_without_raising(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/purge 99999999999999999999 confirm"))
    assert "⚠️" in client.sent[-1][1]
    db.close()


async def test_help_lists_all_commands(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    await handler.on_command(FakeEvent("/help"))
    text = client.sent[-1][1]
    for cmd in ("/watch", "/unwatch", "/list", "/purge", "/help"):
        assert cmd in text
    db.close()


async def test_help_does_not_touch_storage(tmp_path):
    handler, client, db, _calls = make_handler(tmp_path)
    db.add_watch(-1001, "Chat A", 1000)
    await handler.on_command(FakeEvent("/help"))
    assert db.list_watch() == [(-1001, "Chat A", 1000)]
    db.close()
