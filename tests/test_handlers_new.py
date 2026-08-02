import types

import pytest

from config import Config
from handlers import MessageLogger
from storage import Storage

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
        download_media=True,
        max_media_mb=1,
        media_dir=tmp_path / "media",
        db_path=tmp_path / "db.sqlite",
        retention_days=14,
        backfill_on_start=False,
        backfill_limit=10,
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
):
    import datetime

    sender = types.SimpleNamespace(id=sender_id, first_name="Alice", last_name=None,
                                   username="alice", title=None)
    chat = types.SimpleNamespace(id=abs(chat_id), title="My Chat", first_name=None,
                                 last_name=None, username=None)
    message = types.SimpleNamespace(
        id=msg_id,
        message=text,
        text=text,
        media=media,
        file=file,
        date=datetime.datetime.fromtimestamp(date_ts, tz=datetime.timezone.utc),
        out=out,
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
