import types

import pytest
from telethon.tl.types import Chat, User

from config import Config
from handlers import MessageLogger
from storage import Storage, StoredMessage

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self):
        self.sent = []
        self.files = []
        self.downloads = []

    async def send_message(self, target, text, **kw):
        self.sent.append((target, text, kw))

    async def send_file(self, target, file, **kw):
        self.files.append((target, file, kw))

    async def download_media(self, message, file=None):
        self.downloads.append((message, file))
        with open(file, "wb") as fh:
            fh.write(b"data")
        return file

    async def get_messages(self, _entity, ids=None):
        return [None for _ in (ids or [])]

    def add_event_handler(self, *_a, **_kw):
        pass


def make_config(tmp_path, **kw):
    base = dict(
        api_id=1,
        api_hash="h",
        session_name="s",
        watch_chats=(),
        ignore_chats=(),
        log_target="me",
        log_outgoing=True,
        log_edits=True,
        log_joins=True,
        download_media=True,
        max_media_mb=1,
        max_media_total_mb=0,
        media_dir=tmp_path / "media",
        db_path=tmp_path / "db.sqlite",
        retention_days=14,
        backfill_on_start=False,
        backfill_limit=10,
        log_level="INFO",
        log_file=None,
        reconcile_interval_min=0,
        reconcile_window_hours=12,
        proxy_enabled=False,
        proxy_type="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
    )
    base.update(kw)
    return Config(**base)


def make_logger(tmp_path, *, watch=None, ignore=None, **cfg_kw):
    client = FakeClient()
    db = Storage(tmp_path / "db.sqlite")
    logger = MessageLogger(
        client=client,
        storage=db,
        config=make_config(tmp_path, **cfg_kw),
        log_target_id=777,
        watch_ids=watch if watch is not None else set(),
        ignore_ids=ignore if ignore is not None else {777},
    )
    return logger, client, db


def fake_event(
    *,
    chat_id=-1001,
    msg_id=5,
    text="hi",
    is_channel=True,
    out=False,
    sender_id=42,
    media=None,
    file=None,
    date_ts=1700000000,
    edit_date_ts=None,
    reply_to_msg_id=None,
):
    import datetime

    sender = User(
        id=sender_id, is_self=False, contact=False, mutual_contact=False,
        deleted=False, bot=False, bot_chat_history=False, bot_nochats=False,
        verified=False, restricted=False, min=False, bot_inline_geo=False,
        support=False, scam=False, apply_min_photo=False, fake=False,
        first_name="Alice", last_name=None, username="alice",
    )
    chat = Chat(
        id=abs(chat_id), title="My Chat", photo=None,
        participants_count=0, date=None, version=0,
    )
    message = types.SimpleNamespace(
        id=msg_id,
        message=text,
        text=text,
        media=media,
        file=file,
        date=datetime.datetime.fromtimestamp(date_ts, tz=datetime.timezone.utc),
        out=out,
        sender_id=sender_id,
        edit_date=(
            datetime.datetime.fromtimestamp(edit_date_ts, tz=datetime.timezone.utc)
            if edit_date_ts is not None
            else None
        ),
        reply_to_msg_id=reply_to_msg_id,
    )
    return types.SimpleNamespace(
        chat_id=chat_id,
        message=message,
        id=msg_id,
        is_channel=is_channel,
        out=out,
        sender=sender,
        sender_id=sender_id,
        chat=chat,
        text=text,
    )


async def test_new_message_is_stored(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event())
    stored = db.find_message(-1001, 5)
    assert stored is not None
    assert stored.text == "hi"
    assert stored.sender_name == "Alice"
    assert stored.chat_title == "My Chat"
    assert stored.is_channel is True
    db.close()


async def test_dm_message_stored_with_is_channel_false(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(chat_id=555, is_channel=False))
    stored = db.find_message(None, 5)
    assert stored is not None
    assert stored.is_channel is False
    db.close()


async def test_ignored_chat_is_not_stored(tmp_path):
    logger, _client, db = make_logger(tmp_path, ignore={777, -1001})
    await logger.on_new_message(fake_event(chat_id=-1001))
    assert db.find_message(-1001, 5) is None
    db.close()


async def test_log_target_chat_is_never_stored(tmp_path):
    """Reports land in Saved Messages; caching them would create a feedback loop."""
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(chat_id=777))
    assert db.find_message(777, 5) is None
    db.close()


async def test_watch_list_excludes_other_chats(tmp_path):
    logger, _client, db = make_logger(tmp_path, watch={-1002})
    await logger.on_new_message(fake_event(chat_id=-1001))
    await logger.on_new_message(fake_event(chat_id=-1002, msg_id=6))
    assert db.find_message(-1001, 5) is None
    assert db.find_message(-1002, 6) is not None
    db.close()


async def test_outgoing_skipped_when_disabled(tmp_path):
    logger, _client, db = make_logger(tmp_path, log_outgoing=False)
    await logger.on_new_message(fake_event(out=True))
    assert db.find_message(-1001, 5) is None
    db.close()


async def test_outgoing_stored_when_enabled(tmp_path):
    logger, _client, db = make_logger(tmp_path, log_outgoing=True)
    await logger.on_new_message(fake_event(out=True))
    assert db.find_message(-1001, 5) is not None
    db.close()


async def test_media_downloaded_within_limit(tmp_path):
    file = types.SimpleNamespace(size=1024, ext=".jpg", mime_type="image/jpeg")
    logger, client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(media=object(), file=file))
    stored = db.find_message(-1001, 5)
    assert stored.media_path is not None
    assert stored.media_path.endswith(".jpg")
    assert len(client.downloads) == 1
    db.close()


async def test_row_is_cached_before_media_download(tmp_path):
    """Updates are dispatched concurrently: a deletion arriving while the media
    is still downloading must already find the message in the cache."""
    logger, client, db = make_logger(tmp_path)
    seen = {}

    async def watching_download(_message, file=None):
        seen["cached"] = db.find_message(-1001, 5) is not None
        with open(file, "wb") as fh:
            fh.write(b"data")
        return file

    client.download_media = watching_download
    file = types.SimpleNamespace(size=1024, ext=".jpg", mime_type="image/jpeg")
    await logger.on_new_message(fake_event(media=object(), file=file))

    assert seen["cached"] is True
    assert db.find_message(-1001, 5).media_path.endswith(".jpg")
    db.close()


async def test_backfill_does_not_redownload_cached_message(tmp_path):
    """overwrite=False means the row is already final — re-fetching its media
    would burn rate limit for a file that is on disk already."""
    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".jpg", mime_type="image/jpeg")
    event = fake_event(media=object(), file=file)

    await logger.store_message(
        event.message, peer_id=-1001, is_channel=True, chat_title="c", overwrite=False
    )
    await logger.store_message(
        event.message, peer_id=-1001, is_channel=True, chat_title="c", overwrite=False
    )

    assert len(client.downloads) == 1
    db.close()


async def test_media_skipped_over_limit(tmp_path):
    file = types.SimpleNamespace(size=5 * 1024 * 1024, ext=".mp4", mime_type="video/mp4")
    logger, client, db = make_logger(tmp_path, max_media_mb=1)
    await logger.on_new_message(fake_event(media=object(), file=file))
    stored = db.find_message(-1001, 5)
    assert stored.media_path is None
    assert stored.media_type is not None
    assert client.downloads == []
    db.close()


async def test_media_skipped_when_download_disabled(tmp_path):
    file = types.SimpleNamespace(size=10, ext=".jpg", mime_type="image/jpeg")
    logger, client, db = make_logger(tmp_path, download_media=False)
    await logger.on_new_message(fake_event(media=object(), file=file))
    assert db.find_message(-1001, 5).media_path is None
    assert client.downloads == []
    db.close()


async def test_new_message_stores_reply_to_msg_id(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(reply_to_msg_id=1))
    assert db.find_message(-1001, 5).reply_to_msg_id == 1
    db.close()


async def test_new_message_without_reply_stores_none(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event())
    assert db.find_message(-1001, 5).reply_to_msg_id is None
    db.close()


async def test_media_download_records_media_size(tmp_path):
    file = types.SimpleNamespace(size=1024, ext=".jpg", mime_type="image/jpeg")
    logger, _client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(media=object(), file=file))
    assert db.find_message(-1001, 5).media_size == 1024
    db.close()


async def test_media_quota_evicts_oldest_when_exceeded(tmp_path):
    # MAX_MEDIA_TOTAL_MB=1 (1_048_576 bytes): two 700_000-byte downloads push
    # the running total over budget on the second, evicting the first.
    file = types.SimpleNamespace(size=700_000, ext=".jpg", mime_type="image/jpeg")
    logger, client, db = make_logger(tmp_path, max_media_total_mb=1)
    await logger.on_new_message(fake_event(msg_id=1, media=object(), file=file))
    await logger.on_new_message(fake_event(msg_id=2, media=object(), file=file))
    assert db.find_message(-1001, 1).media_path is None
    assert db.find_message(-1001, 2).media_path is not None
    db.close()


async def test_media_quota_disabled_by_default_never_evicts(tmp_path):
    # Same 700_000-byte files that trigger eviction with a 1 MB quota above —
    # with the quota disabled (default 0), nothing gets evicted.
    file = types.SimpleNamespace(size=700_000, ext=".jpg", mime_type="image/jpeg")
    logger, client, db = make_logger(tmp_path, max_media_total_mb=0)
    await logger.on_new_message(fake_event(msg_id=1, media=object(), file=file))
    await logger.on_new_message(fake_event(msg_id=2, media=object(), file=file))
    assert db.find_message(-1001, 1).media_path is not None
    assert db.find_message(-1001, 2).media_path is not None
    db.close()


async def test_enforce_media_quota_unlinks_evicted_file(tmp_path):
    logger, _client, db = make_logger(tmp_path, max_media_total_mb=1)
    media_file = tmp_path / "old.jpg"
    media_file.write_bytes(b"x" * 100)
    db.save_message(
        StoredMessage(
            chat_id=-1001, msg_id=1, is_channel=True, chat_title="c",
            sender_id=None, sender_name=None, text=None,
            media_path=str(media_file), media_type="photo", date=100,
            media_size=2 * 1024 * 1024,  # already over the 1 MB budget
        )
    )
    await logger.enforce_media_quota()
    assert not media_file.exists()
    assert db.find_message(-1001, 1).media_path is None
    db.close()


async def test_send_report_plain_text(tmp_path):
    logger, client, db = make_logger(tmp_path)
    await logger.send_report("hello")
    assert client.sent == [(777, "hello", {"parse_mode": "html", "link_preview": False})]
    db.close()


async def test_send_report_with_media(tmp_path):
    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    logger, client, db = make_logger(tmp_path)
    await logger.send_report("caption", media_path=str(path))
    assert len(client.files) == 1
    target, sent_file, kw = client.files[0]
    assert target == 777
    assert sent_file == str(path)
    assert kw["caption"] == "caption"
    db.close()


async def test_send_report_falls_back_when_media_missing(tmp_path):
    logger, client, db = make_logger(tmp_path)
    await logger.send_report("caption", media_path=str(tmp_path / "gone.jpg"))
    assert client.files == []
    assert len(client.sent) == 1
    db.close()


async def test_sender_name_cached_for_channel_sender(tmp_path):
    """A post from a channel/anon-admin has sender=Channel, whose marked peer id
    differs from its raw id — the name cache must key on the marked id, matching
    how store_message looks names up."""
    from telethon.tl.types import Channel

    logger, _client, db = make_logger(tmp_path)
    channel_sender = Channel(id=555, title="Some Channel", photo=None, date=None)
    event = fake_event(sender_id=None)
    event.sender = channel_sender
    event.message.sender_id = -1000000000555  # utils.get_peer_id(channel_sender)
    event.sender_id = -1000000000555

    await logger.on_new_message(event)
    stored = db.find_message(-1001, 5)
    assert stored.sender_name == "Some Channel"
    db.close()


async def test_send_report_falls_back_to_text_when_caption_too_long(tmp_path):
    from telethon.errors import MediaCaptionTooLongError

    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    logger, client, db = make_logger(tmp_path)

    async def failing_send_file(_target, _file, **_kw):
        raise MediaCaptionTooLongError(request=None)

    client.send_file = failing_send_file
    await logger.send_report("a" * 1200, media_path=str(path))
    assert len(client.sent) == 1
    assert client.sent[0][1] == "a" * 1200
    db.close()


async def test_send_report_retries_as_document_on_bad_photo_dimensions(tmp_path):
    from telethon.errors import PhotoInvalidDimensionsError

    path = tmp_path / "f.jpg"
    path.write_bytes(b"x")
    logger, client, db = make_logger(tmp_path)

    calls = []

    async def flaky_send_file(_target, _file, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise PhotoInvalidDimensionsError(request=None)
        client.files.append((_target, _file, kw))

    client.send_file = flaky_send_file
    ok = await logger.send_report("caption", media_path=str(path))
    assert ok is True
    assert len(calls) == 2
    assert calls[1]["force_document"] is True
    assert len(client.files) == 1
    db.close()


async def test_send_report_returns_false_on_failure(tmp_path):
    logger, client, db = make_logger(tmp_path)

    async def failing_send_message(*_a, **_kw):
        raise RuntimeError("boom")

    client.send_message = failing_send_message
    ok = await logger.send_report("hello")
    assert ok is False
    db.close()


async def test_media_with_unsafe_extension_falls_back_to_bin(tmp_path):
    file = types.SimpleNamespace(size=10, ext="/../../evil", mime_type="application/octet-stream")
    logger, client, db = make_logger(tmp_path)
    await logger.on_new_message(fake_event(media=object(), file=file))
    stored = db.find_message(-1001, 5)
    assert stored.media_path is not None
    assert stored.media_path.endswith(".bin")
    db.close()


def fake_chat_action_event(
    *,
    chat_id=-1001,
    user_joined=False,
    user_added=False,
    user_left=False,
    user_kicked=False,
    users=None,
    added_by=None,
    kicked_by=None,
):
    joined_user = User(
        id=99, is_self=False, contact=False, mutual_contact=False,
        deleted=False, bot=False, bot_chat_history=False, bot_nochats=False,
        verified=False, restricted=False, min=False, bot_inline_geo=False,
        support=False, scam=False, apply_min_photo=False, fake=False,
        first_name="Bob", last_name=None, username="bobby",
    )
    chat = Chat(
        id=abs(chat_id), title="My Chat", photo=None,
        participants_count=0, date=None, version=0,
    )

    async def get_users():
        return users if users is not None else [joined_user]

    async def get_added_by():
        return added_by

    async def get_kicked_by():
        return kicked_by

    return types.SimpleNamespace(
        chat_id=chat_id,
        chat=chat,
        user_joined=user_joined,
        user_added=user_added,
        user_left=user_left,
        user_kicked=user_kicked,
        get_users=get_users,
        get_added_by=get_added_by,
        get_kicked_by=get_kicked_by,
    )


async def test_user_joined_is_reported(tmp_path):
    logger, client, db = make_logger(tmp_path)
    await logger.on_chat_action(fake_chat_action_event(user_joined=True))
    assert len(client.sent) == 1
    text = client.sent[0][1]
    assert "Bob" in text
    assert "Вступление" in text
    db.close()


async def test_user_added_includes_actor(tmp_path):
    adder = User(
        id=7, is_self=False, contact=False, mutual_contact=False,
        deleted=False, bot=False, bot_chat_history=False, bot_nochats=False,
        verified=False, restricted=False, min=False, bot_inline_geo=False,
        support=False, scam=False, apply_min_photo=False, fake=False,
        first_name="Carl", last_name=None, username="carl",
    )
    logger, client, db = make_logger(tmp_path)
    await logger.on_chat_action(fake_chat_action_event(user_added=True, added_by=adder))
    text = client.sent[0][1]
    assert "Bob" in text
    assert "Carl" in text
    assert "Добавил" in text
    db.close()


async def test_user_left_is_reported(tmp_path):
    logger, client, db = make_logger(tmp_path)
    await logger.on_chat_action(fake_chat_action_event(user_left=True))
    text = client.sent[0][1]
    assert "Выход" in text
    db.close()


async def test_user_kicked_includes_actor(tmp_path):
    kicker = User(
        id=8, is_self=False, contact=False, mutual_contact=False,
        deleted=False, bot=False, bot_chat_history=False, bot_nochats=False,
        verified=False, restricted=False, min=False, bot_inline_geo=False,
        support=False, scam=False, apply_min_photo=False, fake=False,
        first_name="Dana", last_name=None, username="dana",
    )
    logger, client, db = make_logger(tmp_path)
    await logger.on_chat_action(fake_chat_action_event(user_kicked=True, kicked_by=kicker))
    text = client.sent[0][1]
    assert "Выход" in text
    assert "Dana" in text
    assert "Удалил" in text
    db.close()


async def test_join_not_reported_when_log_joins_disabled(tmp_path):
    logger, client, db = make_logger(tmp_path, log_joins=False)
    await logger.on_chat_action(fake_chat_action_event(user_joined=True))
    assert client.sent == []
    db.close()


async def test_join_in_untracked_chat_is_ignored(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1002})
    await logger.on_chat_action(fake_chat_action_event(chat_id=-1001, user_joined=True))
    assert client.sent == []
    db.close()


async def test_non_membership_action_is_ignored(tmp_path):
    """new_title/new_photo events also fire ChatAction; only membership changes report."""
    logger, client, db = make_logger(tmp_path)
    await logger.on_chat_action(fake_chat_action_event())
    assert client.sent == []
    db.close()


async def test_custom_logger_is_used_for_internal_logging(tmp_path, caplog):
    custom = __import__("logging").getLogger("acct.test1")
    client = FakeClient()
    db = Storage(tmp_path / "db.sqlite")
    cfg = make_config(tmp_path, max_media_mb=1)
    logger = MessageLogger(
        client=client, storage=db, config=cfg, log_target_id=777,
        watch_ids=set(), ignore_ids={777}, log=custom,
    )
    assert logger.log is custom

    file = types.SimpleNamespace(size=5 * 1024 * 1024, ext=".mp4", mime_type="video/mp4")
    with caplog.at_level("INFO", logger="acct.test1"):
        await logger.on_new_message(fake_event(media=object(), file=file))
    assert any(r.name == "acct.test1" for r in caplog.records)
    db.close()
