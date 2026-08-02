import types

import pytest

from storage import StoredMessage
from tests.test_handlers_new import make_logger

pytestmark = pytest.mark.asyncio


def delete_event(deleted_ids, chat_id=None):
    return types.SimpleNamespace(deleted_ids=deleted_ids, chat_id=chat_id)


def seed(db, *, chat_id, msg_id, is_channel, text="hello", media_path=None):
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=is_channel,
            chat_title="My Chat",
            sender_id=42,
            sender_name="Alice",
            text=text,
            media_path=media_path,
            media_type="MessageMediaPhoto" if media_path else None,
            date=1700000000,
            deleted=False,
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


async def test_batch_deletion_reports_each(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="one")
    seed(db, chat_id=-1001, msg_id=2, is_channel=True, text="two")
    await logger.on_message_deleted(delete_event([1, 2], chat_id=-1001))
    assert len(client.sent) == 2
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


async def test_batch_with_missing_and_present(tmp_path):
    """One id in the batch was never cached; the other was — the cached one
    must still be reported."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=1, is_channel=True, text="present")
    await logger.on_message_deleted(delete_event([999, 1], chat_id=-1001))
    assert len(client.sent) == 1
    assert "present" in client.sent[0][1]
    db.close()
