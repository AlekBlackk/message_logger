import asyncio
import types

import pytest

from storage import StoredMessage
from tests.test_handlers_new import make_logger

pytestmark = pytest.mark.asyncio


def delete_event(deleted_ids, chat_id=None):
    return types.SimpleNamespace(deleted_ids=deleted_ids, chat_id=chat_id)


def _body_text(sent_entry):
    """The message body is the last line of a deletion report."""
    return sent_entry[1].split("\n")[-1]


def seed(
    db,
    *,
    chat_id,
    msg_id,
    is_channel,
    chat_title="My Chat",
    text="hello",
    media_path=None,
    media_type=None,
    reply_to_msg_id=None,
    sender_id=42,
    sender_name="Alice",
    date=1700000000,
    deleted=False,
):
    actual_media_type = (
        media_type
        if media_type is not None
        else ("MessageMediaPhoto" if media_path else None)
    )
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=is_channel,
            chat_title=chat_title,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            media_path=media_path,
            media_type=actual_media_type,
            date=date,
            deleted=deleted,
            reply_to_msg_id=reply_to_msg_id,
        )
    )


async def test_channel_deletion_reports(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True, text="secret")
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert len(client.sent) == 1
    assert "secret" in client.sent[0][1]
    db.close()


async def test_deletion_marks_row(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True)
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_dm_deletion_without_chat_id(tmp_path):
    """chat_id is None for DMs and basic groups — lookup falls back to msg_id."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=555, msg_id=9, is_channel=False, text="dm text")
    await logger.on_message_deleted(delete_event([9], chat_id=None))
    assert len(client.sent) == 1
    assert "dm text" in client.sent[0][1]
    db.close()


async def test_dm_deletion_does_not_match_channel_row(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=9, is_channel=True, text="channel text")
    await logger.on_message_deleted(delete_event([9], chat_id=None))
    assert client.sent == []
    db.close()


async def test_unknown_message_is_silent(tmp_path):
    logger, client, db = make_logger(tmp_path)
    await logger.on_message_deleted(delete_event([123], chat_id=-1001))
    assert client.sent == []
    assert client.files == []
    db.close()


async def test_deletion_with_media_sends_file(tmp_path):
    media = tmp_path / "pic.jpg"
    media.write_bytes(b"x")
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True, media_path=str(media))
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert len(client.files) == 1
    assert client.files[0][1] == str(media)
    assert client.sent == []
    db.close()


async def test_deletion_in_ignored_chat_is_silent(tmp_path):
    logger, client, db = make_logger(tmp_path, ignore={777, -1001})
    seed(db, chat_id=-1001, msg_id=5, is_channel=True)
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert client.sent == []
    db.close()


async def test_own_report_deletion_is_silent(tmp_path):
    """Deleting a report in Saved Messages must not spawn another report."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=777, msg_id=5, is_channel=False)
    await logger.on_message_deleted(delete_event([5], chat_id=777))
    assert client.sent == []
    db.close()


async def test_batch_deletion_reports_each_sender(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="one", sender_id=42)
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="two", sender_id=43)
    await logger.on_message_deleted(delete_event([1, 2], chat_id=-1001))
    assert len(client.sent) == 2
    db.close()


async def test_batch_deletion_same_sender_reports_every_message(tmp_path):
    """A ban wipes every message the banned user sent — each one gets its own
    report, ordered newest → oldest by date."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="oldest", date=1000)
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="newest", date=3000)
    seed(db, chat_id=-1001, msg_id=3, is_channel=True, text="middle", date=2000)
    await logger.on_message_deleted(delete_event([1, 2, 3], chat_id=-1001))
    assert [_body_text(s) for s in client.sent] == ["newest", "middle", "oldest"]
    for i in (1, 2, 3):
        assert db.find_message(-1001, i).deleted is True
    db.close()


async def test_batch_same_sender_self_reply_chain_all_reported(tmp_path):
    """A banned user's replies to their own earlier messages are a normal
    conversation, not a deleted-post cascade — every message is still reported."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="first", date=1000)
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="reply to self",
         date=2000, reply_to_msg_id=1)
    await logger.on_message_deleted(delete_event([1, 2], chat_id=-1001))
    assert {_body_text(s) for s in client.sent} == {"first", "reply to self"}
    db.close()


async def test_batch_multiple_senders_reported_newest_first(tmp_path):
    """Several senders hit in one delete event: reports go out newest → oldest
    so the freshest sits at the top of the log."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="older alice",
         sender_id=42, date=5000)
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="newer bob",
         sender_id=43, date=9000)
    await logger.on_message_deleted(delete_event([1, 2], chat_id=-1001))
    assert len(client.sent) == 2
    assert "newer bob" in client.sent[0][1]
    assert "older alice" in client.sent[1][1]
    db.close()


async def test_batch_over_report_cap_keeps_newest_and_silences_rest(tmp_path):
    """A ban that wiped more messages than the cap: only the cap's worth of
    reports go out (newest first), the rest are marked deleted without a report
    so reconcile stays quiet."""
    logger, client, db = make_logger(tmp_path)
    for i in range(1, 14):  # 13 messages from one banned user, date ascending
        seed(db, chat_id=-1001, msg_id=i, is_channel=True, text=f"msg{i}",
             date=1000 + i)
    await logger.on_message_deleted(delete_event(list(range(1, 14)), chat_id=-1001))
    assert len(client.sent) == 10
    assert "msg13" in client.sent[0][1]
    assert "msg4" in client.sent[9][1]
    for i in (1, 2, 3):
        assert db.find_message(-1001, i).deleted is True
    db.close()


async def test_batch_over_report_cap_logs_warning(tmp_path, caplog):
    import logging

    logger, _client, db = make_logger(tmp_path)
    for i in range(1, 13):
        seed(db, chat_id=-1001, msg_id=i, is_channel=True, date=1000 + i)
    with caplog.at_level(logging.WARNING, logger="handlers"):
        await logger.on_message_deleted(delete_event(list(range(1, 13)), chat_id=-1001))
    assert any(
        "cap" in r.message and r.levelname == "WARNING" for r in caplog.records
    )
    db.close()


async def test_already_deleted_is_not_reported_twice(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True)
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert len(client.sent) == 1
    db.close()


async def test_batch_continues_after_one_item_raises(tmp_path):
    """The whole point of moving try/except inside the loop: one bad lookup
    must not swallow the rest of the batch."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="one")
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="two")

    original_find = db.find_message

    def flaky_find(chat_id, msg_id):
        if msg_id == 1:
            raise RuntimeError("boom")
        return original_find(chat_id, msg_id)

    db.find_message = flaky_find

    await logger.on_message_deleted(delete_event([1, 2], chat_id=-1001))
    assert len(client.sent) == 1
    assert "two" in client.sent[0][1]
    db.close()


async def test_mixed_batch_trackable_and_ignored(tmp_path):
    """DM/basic-group deletions carry no chat_id, so a single batch can mix
    messages from a tracked chat and an ignored one — each must be filtered
    independently after lookup."""
    logger, client, db = make_logger(tmp_path, ignore={777, -2002})
    seed(db, chat_id=-1001, msg_id=1, is_channel=False, text="keep")
    seed(db, chat_id=-2002, msg_id=2, is_channel=False, text="ignored")
    await logger.on_message_deleted(delete_event([1, 2], chat_id=None))
    assert len(client.sent) == 1
    assert "keep" in client.sent[0][1]
    db.close()


async def test_failed_send_is_not_logged_as_reported(tmp_path, caplog):
    """send_report failing (e.g. Telegram rejects the upload) must not be
    logged as a successful delivery."""
    import logging

    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True, text="secret")

    async def failing_send_message(*_a, **_kw):
        raise RuntimeError("boom")

    client.send_message = failing_send_message
    with caplog.at_level(logging.INFO, logger="handlers"):
        await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert not any("deletion reported" in r.message for r in caplog.records)
    assert any(
        "NOT reported" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    )
    db.close()


async def test_failed_send_leaves_row_undeleted_for_retry(tmp_path):
    """Marking the row deleted before the report is out would hide it from the
    reconcile sweep, making a failed send a permanent loss."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True)

    async def failing_send_message(*_a, **_kw):
        raise RuntimeError("boom")

    client.send_message = failing_send_message
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert db.find_message(-1001, 5).deleted is False
    db.close()


async def test_uncached_deletion_in_known_chat_warns(tmp_path, caplog):
    """A cache miss in a chat we do track is the symptom of a caching hole —
    at DEBUG it never reaches the log and the hole stays invisible."""
    import logging

    logger, _client, db = make_logger(tmp_path)
    with caplog.at_level(logging.WARNING, logger="handlers"):
        await logger.on_message_deleted(delete_event([123], chat_id=-1001))
    assert any(
        "not in cache" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    )
    db.close()


async def test_deletion_report_includes_reply_context(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="original")
    seed(db, chat_id=-1001, msg_id=5, is_channel=True, text="secret", reply_to_msg_id=1)
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert "В ответ на" in client.sent[0][1]
    assert "original" in client.sent[0][1]
    db.close()


async def test_deletion_report_omits_reply_context_when_parent_uncached(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, is_channel=True, text="secret", reply_to_msg_id=999)
    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    assert "В ответ на" not in client.sent[0][1]
    db.close()


async def test_batch_deletion_suppresses_comments_of_deleted_post(tmp_path):
    """Deleting a channel post cascades into Telegram deleting its discussion-
    group comments in the same batch — those comments must be marked deleted
    silently, not reported one by one, only the post itself is reported."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="the post", sender_id=None)
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="comment one",
         reply_to_msg_id=1, sender_id=42)
    seed(db, chat_id=-1001, msg_id=3, is_channel=True, text="comment two",
         reply_to_msg_id=1, sender_id=43)
    await logger.on_message_deleted(delete_event([1, 2, 3], chat_id=-1001))
    assert len(client.sent) == 1
    assert "the post" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    assert db.find_message(-1001, 3).deleted is True
    db.close()


async def test_batch_with_missing_and_present(tmp_path):
    """One id in the batch was never cached; the other was — the cached one
    must still be reported."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="present")
    await logger.on_message_deleted(delete_event([999, 1], chat_id=-1001))
    assert len(client.sent) == 1
    assert "present" in client.sent[0][1]
    db.close()


async def test_batch_deletion_suppresses_reply_when_parent_recently_deleted(tmp_path):
    """If a parent message was deleted in a preceding chunk of a ban cascade,
    a reply to it by a different user in a subsequent delete batch is collateral
    and must be marked deleted silently."""
    logger, client, db = make_logger(tmp_path)
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="parent from yesterday",
        sender_id=99,
        date=1700000000 - 86400,
        deleted=True,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        is_channel=True,
        text="reply from yesterday",
        sender_id=42,
        reply_to_msg_id=1,
        date=1700000000 - 86400,
        deleted=False,
    )
    logger.record_deletion(-1001, 1)
    await logger.on_message_deleted(delete_event([2], chat_id=-1001))
    assert client.sent == []
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_batch_deletion_reports_reply_when_parent_deleted_long_ago(tmp_path):
    """If a parent message was deleted in the past (not part of the current cascade),
    a user deleting their own reply is an intentional deletion and must be reported."""
    logger, client, db = make_logger(tmp_path)
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="old deleted parent",
        sender_id=99,
        date=1700000000 - 86400,
        deleted=True,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        is_channel=True,
        text="my reply",
        sender_id=42,
        reply_to_msg_id=1,
        date=1700000000 - 86400,
        deleted=False,
    )
    await logger.on_message_deleted(delete_event([2], chat_id=-1001))
    assert len(client.sent) == 1
    assert "my reply" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_batch_deletion_suppresses_reply_to_banned_user_from_previous_day(tmp_path):
    """When a banned user's messages are wiped, replies to their earlier messages
    (even if the parent was on the previous day and not in by_id) are suppressed,
    while the banned user's own messages are reported."""
    logger, client, db = make_logger(tmp_path)
    # Banned user's post from yesterday
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="banned user yesterday",
        sender_id=99,
        date=1700000000 - 86400,
    )
    # Victim's reply from yesterday
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        is_channel=True,
        text="victim reply yesterday",
        sender_id=42,
        reply_to_msg_id=1,
        date=1700000000 - 86400,
    )
    # Banned user's post today
    seed(
        db,
        chat_id=-1001,
        msg_id=3,
        is_channel=True,
        text="banned user today",
        sender_id=99,
        date=1700000000,
    )
    # Batch deletes today's banned user message (3) and the reply (2)
    await logger.on_message_deleted(delete_event([3, 2], chat_id=-1001))
    assert len(client.sent) == 1
    assert "banned user today" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    assert db.find_message(-1001, 3).deleted is True
    db.close()


async def test_batch_deletion_reports_manual_reply_deletion_when_parent_alive(tmp_path):
    """If the parent message is still alive (not deleted), a user deleting their own
    reply is an intentional deletion and must be reported."""
    logger, client, db = make_logger(tmp_path)
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="living parent",
        sender_id=99,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        is_channel=True,
        text="my reply",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    await logger.on_message_deleted(delete_event([2], chat_id=-1001))
    assert len(client.sent) == 1
    assert "my reply" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_batch_deletion_suppresses_multi_level_reply_chain(tmp_path):
    """A banned user's post has a reply from user B, which in turn has a reply
    from user C. Both replies are collateral and must be suppressed."""
    logger, client, db = make_logger(tmp_path)
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="banned user root",
        sender_id=99,
        date=1700000000,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        is_channel=True,
        text="reply by bob",
        sender_id=42,
        reply_to_msg_id=1,
        date=1700000001,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=3,
        is_channel=True,
        text="reply by charlie to bob",
        sender_id=43,
        reply_to_msg_id=2,
        date=1700000002,
    )
    await logger.on_message_deleted(delete_event([1, 2, 3], chat_id=-1001))
    assert len(client.sent) == 1
    assert "banned user root" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    assert db.find_message(-1001, 3).deleted is True
    db.close()


async def test_deletion_waits_for_inflight_download_and_attaches_media(tmp_path):
    import asyncio
    from tests.test_handlers_new import fake_event

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=10, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def slow_download(message, file=None):
        download_started.set()
        await release_download.wait()
        with open(file, "wb") as fh:
            fh.write(b"x" * 1024)
        return file

    client.download_media = slow_download

    # Start message ingestion in background — fetch_media will pause
    new_task = asyncio.create_task(logger.on_new_message(event))
    await download_started.wait()

    # The row exists in DB, but media_path is None yet
    stored_before = db.find_message(-1001, 10)
    assert stored_before is not None
    assert stored_before.media_path is None

    # Deletion event arrives while download is still running
    del_task = asyncio.create_task(logger.on_message_deleted(delete_event([10], chat_id=-1001)))
    await asyncio.sleep(0.02)

    # Deletion has not sent report yet because it is waiting for download
    assert client.sent == []
    assert client.files == []

    # Now allow download to finish
    release_download.set()
    await new_task
    await del_task

    # Report must be sent WITH media file, not text-only
    assert client.sent == []
    assert len(client.files) == 1
    _target, sent_path, kw = client.files[0]
    assert "10.mp4" in sent_path
    assert db.find_message(-1001, 10).deleted is True
    db.close()


async def test_deletion_inflight_download_timeout(tmp_path, monkeypatch):
    import asyncio
    import handlers
    from tests.test_handlers_new import fake_event

    monkeypatch.setattr(handlers, "DOWNLOAD_TIMEOUT_SEC", 0.05)

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=11, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def hung_download(message, file=None):
        download_started.set()
        await release_download.wait()

    client.download_media = hung_download

    new_task = asyncio.create_task(logger.on_new_message(event))
    await download_started.wait()

    # Deletion arrives, timeout should trigger and send text-only report without hanging
    del_task = asyncio.create_task(logger.on_message_deleted(delete_event([11], chat_id=-1001)))
    await del_task

    assert len(client.sent) == 1
    assert client.files == []
    assert db.find_message(-1001, 11).deleted is True

    # Cleanup hanging task
    release_download.set()
    await new_task
    db.close()


async def test_deletion_inflight_download_error(tmp_path):
    import asyncio
    from tests.test_handlers_new import fake_event

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=12, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def failing_download(message, file=None):
        download_started.set()
        await release_download.wait()
        raise RuntimeError("network down")

    client.download_media = failing_download

    new_task = asyncio.create_task(logger.on_new_message(event))
    await download_started.wait()

    del_task = asyncio.create_task(logger.on_message_deleted(delete_event([12], chat_id=-1001)))
    release_download.set()

    await new_task
    await del_task

    assert len(client.sent) == 1
    assert client.files == []
    assert db.find_message(-1001, 12).deleted is True
    db.close()


async def test_deletion_gif_includes_animated_attribute(tmp_path):
    from telethon.tl.types import DocumentAttributeAnimated

    media = tmp_path / "anim.mp4"
    media.write_bytes(b"x")
    logger, client, db = make_logger(tmp_path)
    db.save_message(
        StoredMessage(
            chat_id=-1001,
            msg_id=15,
            is_channel=True,
            chat_title="My Chat",
            sender_id=42,
            sender_name="Alice",
            text="funny gif",
            media_path=str(media),
            media_type="гиф",
            date=1700000000,
            deleted=False,
            reply_to_msg_id=None,
        )
    )
    await logger.on_message_deleted(delete_event([15], chat_id=-1001))
    assert len(client.files) == 1
    _target, _file, kw = client.files[0]
    attrs = kw.get("attributes")
    assert attrs is not None
    assert any(isinstance(a, DocumentAttributeAnimated) for a in attrs)
    db.close()


async def test_deletion_mixed_batch_one_timeout_one_success(tmp_path, monkeypatch):
    import asyncio
    import handlers
    from tests.test_handlers_new import fake_event

    monkeypatch.setattr(handlers, "DOWNLOAD_TIMEOUT_SEC", 0.05)

    logger, client, db = make_logger(tmp_path)
    file1 = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    file2 = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    ev1 = fake_event(msg_id=21, media=object(), file=file1)
    ev2 = fake_event(msg_id=22, media=object(), file=file2)

    download21_started = asyncio.Event()
    release21 = asyncio.Event()

    async def mixed_download(message, file=None):
        if message.id == 21:
            download21_started.set()
            await release21.wait()
            with open(file, "wb") as fh:
                fh.write(b"1" * 1024)
            return file
        with open(file, "wb") as fh:
            fh.write(b"2" * 1024)
        return file

    client.download_media = mixed_download

    task21 = asyncio.create_task(logger.on_new_message(ev1))
    await download21_started.wait()

    # msg 22 finishes download immediately
    await logger.on_new_message(ev2)

    # Batch deletion containing both 21 (hanging) and 22 (ready)
    del_task = asyncio.create_task(
        logger.on_message_deleted(delete_event([21, 22], chat_id=-1001))
    )
    await del_task

    # 21 timed out -> text report; 22 succeeded -> media file report
    assert len(client.sent) == 1
    assert len(client.files) == 1
    _target, sent_path, _kw = client.files[0]
    assert "22.mp4" in sent_path

    assert db.find_message(-1001, 21).deleted is True
    assert db.find_message(-1001, 22).deleted is True

    release21.set()
    await task21
    db.close()


async def test_deletion_concurrent_store_message_deduplication(tmp_path):
    import asyncio
    from tests.test_handlers_new import fake_event

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=30, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()
    download_calls = []

    async def slow_download(message, file=None):
        download_calls.append(message.id)
        download_started.set()
        await release_download.wait()
        with open(file, "wb") as fh:
            fh.write(b"x" * 1024)
        return file

    client.download_media = slow_download

    task1 = asyncio.create_task(
        logger.store_message(event.message, peer_id=-1001, is_channel=True, chat_title="c")
    )
    await download_started.wait()

    # Second concurrent store_message for the same message
    task2 = asyncio.create_task(
        logger.store_message(event.message, peer_id=-1001, is_channel=True, chat_title="c")
    )

    release_download.set()
    await task1
    await task2

    # Download must only have executed once
    assert len(download_calls) == 1
    stored = db.find_message(-1001, 30)
    assert stored is not None
    assert stored.media_path is not None
    assert "30.mp4" in stored.media_path
    db.close()


async def test_report_deleted_skips_if_already_deleted_while_waiting(tmp_path):
    import asyncio
    from tests.test_handlers_new import fake_event

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=31, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def slow_download(message, file=None):
        download_started.set()
        await release_download.wait()
        with open(file, "wb") as fh:
            fh.write(b"x" * 1024)
        return file

    client.download_media = slow_download

    new_task = asyncio.create_task(logger.on_new_message(event))
    await download_started.wait()

    stored = db.find_message(-1001, 31)
    assert stored is not None

    del_task = asyncio.create_task(logger.report_deleted(stored, source="live"))
    await asyncio.sleep(0.01)

    # Concurrently marked deleted (e.g. by another handler) while waiting for download
    db.mark_deleted(-1001, 31)

    release_download.set()
    await new_task
    sent = await del_task

    # Report must NOT be sent
    assert sent is False
    assert client.sent == []
    assert client.files == []
    db.close()


async def test_reconcile_waits_for_inflight_download_and_attaches_media(tmp_path):
    import asyncio
    from tests.test_handlers_new import fake_event

    logger, client, db = make_logger(tmp_path)
    file = types.SimpleNamespace(size=1024, ext=".mp4", mime_type="video/mp4")
    event = fake_event(msg_id=32, media=object(), file=file)

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def slow_download(message, file=None):
        download_started.set()
        await release_download.wait()
        with open(file, "wb") as fh:
            fh.write(b"x" * 1024)
        return file

    client.download_media = slow_download

    new_task = asyncio.create_task(logger.on_new_message(event))
    await download_started.wait()

    # Reconcile sees the message is gone on Telegram (returns [None])
    async def get_messages(_entity, ids=None):
        return [None for _ in (ids or [])]

    client.get_messages = get_messages

    rec_task = asyncio.create_task(logger.reconcile_chat(-1001, since_ts=0, budget=10))
    await asyncio.sleep(0.01)

    # Release download so reconcile can finish
    release_download.set()
    await new_task
    reported = await rec_task

    assert reported == 1
    assert len(client.files) == 1
    _target, sent_path, kw = client.files[0]
    assert "32.mp4" in sent_path
    assert db.find_message(-1001, 32).deleted is True
    db.close()


async def test_deletion_sends_reply_media(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent-photo")
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        text="parent text",
        media_path=str(parent_media),
        media_type="фото",
        sender_name="Alice",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert "child text" in client.sent[0][1]
    assert len(client.files) == 1
    target, file_path, kw = client.files[0]
    assert target == logger.log_target_id
    assert file_path == str(parent_media)
    assert kw["reply_to"] == 1
    assert kw["caption"] == "↩️ В ответ на фото от Alice"
    assert kw["parse_mode"] == "html"
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_sends_reply_media_when_both_have_media(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.mp4"
    parent_media.write_bytes(b"parent-video")
    child_media = tmp_path / "child.jpg"
    child_media.write_bytes(b"child-photo")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(parent_media),
        media_type="видео",
        sender_name="Bob",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        media_path=str(child_media),
        media_type="фото",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert client.sent == []
    assert len(client.files) == 2
    # First file: child deletion report with caption
    assert client.files[0][1] == str(child_media)
    assert "Удалённое сообщение" in client.files[0][2]["caption"]
    # Second file: parent media replied to first file
    assert client.files[1][1] == str(parent_media)
    assert client.files[1][2]["reply_to"] == 1
    assert client.files[1][2]["caption"] == "↩️ В ответ на видео от Bob"
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_skips_reply_media_when_config_disabled(tmp_path):
    logger, client, db = make_logger(tmp_path, log_reply_media=False)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent-photo")
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(parent_media),
        media_type="фото",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert client.files == []
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_skips_reply_media_when_file_missing(tmp_path):
    logger, client, db = make_logger(tmp_path)
    missing_media = tmp_path / "missing.jpg"
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(missing_media),
        media_type="фото",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert client.files == []
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_reply_media_video_note_no_caption(tmp_path):
    logger, client, db = make_logger(tmp_path)
    note_media = tmp_path / "note.mp4"
    note_media.write_bytes(b"note-content")
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(note_media),
        media_type="видеосообщение",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert len(client.files) == 1
    _target, file_path, kw = client.files[0]
    assert file_path == str(note_media)
    assert kw.get("reply_to") == 1
    assert "caption" not in kw or kw.get("caption") is None
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_reply_media_sticker_no_caption(tmp_path):
    logger, client, db = make_logger(tmp_path)
    sticker_media = tmp_path / "sticker.webp"
    sticker_media.write_bytes(b"sticker-content")
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(sticker_media),
        media_type="стикер",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert len(client.files) == 1
    _target, file_path, kw = client.files[0]
    assert file_path == str(sticker_media)
    assert kw.get("reply_to") == 1
    assert "caption" not in kw or kw.get("caption") is None
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_reply_media_error_isolation(tmp_path, caplog):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent-photo")
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(parent_media),
        media_type="фото",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    async def failing_send_file(_target, _file, **_kw):
        raise RuntimeError("Telegram media upload exploded")

    client.send_file = failing_send_file

    with caplog.at_level("ERROR"):
        await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert db.find_message(-1001, 5).deleted is True
    assert any("failed to send reply media" in r.message for r in caplog.records)
    db.close()


async def test_deletion_waits_for_parent_inflight_download(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent-bytes")

    # Parent message cached with media_path=None initially
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=None,
        media_type="фото",
        sender_name="Alice",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    download_event = asyncio.Event()
    logger._inflight_downloads[(-1001, 1)] = download_event

    del_task = asyncio.create_task(
        logger.on_message_deleted(delete_event([5], chat_id=-1001))
    )
    await asyncio.sleep(0.02)

    # Deletion task is waiting on download_event; no report yet
    assert client.sent == []
    assert client.files == []

    # Finish background download
    db.update_media_path(-1001, 1, str(parent_media), len(b"parent-bytes"))
    download_event.set()
    await del_task

    assert len(client.sent) == 1
    assert len(client.files) == 1
    assert client.files[0][1] == str(parent_media)
    assert client.files[0][2]["reply_to"] == 1
    assert client.files[0][2]["caption"] == "↩️ В ответ на фото от Alice"
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_skips_if_already_deleted_while_waiting_for_parent_download(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent-bytes")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=None,
        media_type="фото",
        sender_name="Alice",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text",
        reply_to_msg_id=1,
    )

    download_event = asyncio.Event()
    logger._inflight_downloads[(-1001, 1)] = download_event

    stored = db.find_message(-1001, 5)
    del_task = asyncio.create_task(
        logger.report_deleted(stored, source="live")
    )
    await asyncio.sleep(0.02)

    # Concurrently reported/marked deleted by another pass (e.g. reconcile)
    db.mark_deleted(-1001, 5)

    # Finish parent download
    db.update_media_path(-1001, 1, str(parent_media), len(b"parent-bytes"))
    download_event.set()
    sent = await del_task

    # Must return False and send NO reports
    assert sent is False
    assert client.sent == []
    assert client.files == []
    db.close()


async def test_deletion_reply_media_author_fallback_to_cache_and_channel(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media1 = tmp_path / "parent1.jpg"
    parent_media1.write_bytes(b"parent1")
    parent_media2 = tmp_path / "parent2.jpg"
    parent_media2.write_bytes(b"parent2")

    # Parent 1: sender_name in DB is None, but present in logger._names
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(parent_media1),
        media_type="фото",
        sender_id=777,
        sender_name=None,
    )
    logger._names[777] = "CachedAlice"

    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child text 1",
        reply_to_msg_id=1,
    )

    # Parent 2: channel post, sender_name is None, chat_title is "News Channel"
    seed(
        db,
        chat_id=-1002,
        msg_id=10,
        is_channel=True,
        chat_title="News Channel",
        media_path=str(parent_media2),
        media_type="видео",
        sender_id=None,
        sender_name=None,
    )
    seed(
        db,
        chat_id=-1002,
        msg_id=15,
        is_channel=True,
        text="child text 2",
        reply_to_msg_id=10,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))
    await logger.on_message_deleted(delete_event([15], chat_id=-1002))

    assert len(client.files) == 2
    # First: from CachedAlice
    assert client.files[0][2]["caption"] == "↩️ В ответ на фото от CachedAlice"
    # Second: from News Channel
    assert client.files[1][2]["caption"] == "↩️ В ответ на видео от News Channel"
    db.close()


async def test_deletion_reply_media_video_note_flag(tmp_path):
    logger, client, db = make_logger(tmp_path)
    note_media = tmp_path / "round.mp4"
    note_media.write_bytes(b"round")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(note_media),
        media_type="кружочек",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.files) == 1
    _target, _path, kw = client.files[0]
    assert kw.get("video_note") is True
    assert "caption" not in kw or kw.get("caption") is None
    db.close()


async def test_deletion_reply_media_voice_note(tmp_path):
    logger, client, db = make_logger(tmp_path)
    voice_media = tmp_path / "voice.ogg"
    voice_media.write_bytes(b"voice")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(voice_media),
        media_type="голос",
        sender_name="David",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child",
        reply_to_msg_id=1,
    )

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.files) == 1
    _target, _path, kw = client.files[0]
    assert kw.get("voice_note") is True
    assert kw.get("caption") == "↩️ В ответ на голос от David"
    db.close()


async def test_deletion_reply_media_photo_invalid_dimensions(tmp_path):
    from telethon.errors import PhotoInvalidDimensionsError

    logger, client, db = make_logger(tmp_path)
    bad_photo = tmp_path / "bad.jpg"
    bad_photo.write_bytes(b"bad-photo")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(bad_photo),
        media_type="фото",
        sender_name="Eve",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child",
        reply_to_msg_id=1,
    )

    original_send_file = client.send_file
    calls = []

    async def flaky_send_file(target, file, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise PhotoInvalidDimensionsError(request=None)
        return await original_send_file(target, file, **kw)

    client.send_file = flaky_send_file

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(calls) == 2
    assert calls[1].get("force_document") is True
    assert calls[1].get("caption") == "↩️ В ответ на фото от Eve"
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_deletion_reply_media_when_sent_msg_is_list(tmp_path):
    logger, client, db = make_logger(tmp_path)
    parent_media = tmp_path / "parent.jpg"
    parent_media.write_bytes(b"parent")

    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        is_channel=True,
        media_path=str(parent_media),
        media_type="фото",
        sender_name="Frank",
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=5,
        is_channel=True,
        text="child",
        reply_to_msg_id=1,
    )

    async def send_message_returning_list(target, text, **kw):
        client.sent.append((target, text, kw))
        return [types.SimpleNamespace(id=99, text=text, **kw)]

    client.send_message = send_message_returning_list

    await logger.on_message_deleted(delete_event([5], chat_id=-1001))

    assert len(client.sent) == 1
    assert len(client.files) == 1
    assert client.files[0][2]["reply_to"] == 99
    db.close()



