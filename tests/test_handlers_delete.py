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
    text="hello",
    media_path=None,
    reply_to_msg_id=None,
    sender_id=42,
    date=1700000000,
    deleted=False,
):
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=is_channel,
            chat_title="My Chat",
            sender_id=sender_id,
            sender_name="Alice",
            text=text,
            media_path=media_path,
            media_type="MessageMediaPhoto" if media_path else None,
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

