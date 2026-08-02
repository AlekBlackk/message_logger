import pytest

from storage import StoredMessage
from tests.test_handlers_new import fake_event, make_logger

pytestmark = pytest.mark.asyncio


def seed(db, text="old", chat_id=-1001, msg_id=5, is_channel=True):
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=is_channel,
            chat_title="My Chat",
            sender_id=42,
            sender_name="Alice",
            text=text,
            media_path=None,
            media_type=None,
            date=1700000000,
            deleted=False,
        )
    )


async def test_edit_records_history(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    seed(db, text="old")
    await logger.on_message_edited(fake_event(text="new"))
    assert db.get_edits(-1001, 5) == [("old", "new", 1700000000)]
    db.close()


async def test_edit_updates_stored_text(tmp_path):
    logger, _client, db = make_logger(tmp_path)
    seed(db, text="old")
    await logger.on_message_edited(fake_event(text="new"))
    assert db.find_message(-1001, 5).text == "new"
    db.close()


async def test_edit_sends_report(tmp_path):
    logger, client, db = make_logger(tmp_path)
    seed(db, text="old")
    await logger.on_message_edited(fake_event(text="new"))
    assert len(client.sent) == 1
    body = client.sent[0][1]
    assert "old" in body
    assert "new" in body
    db.close()


async def test_edit_report_suppressed_when_disabled(tmp_path):
    logger, client, db = make_logger(tmp_path, log_edits=False)
    seed(db, text="old")
    await logger.on_message_edited(fake_event(text="new"))
    assert client.sent == []
    assert db.get_edits(-1001, 5) != []
    db.close()


async def test_edit_of_unknown_message_stores_it(tmp_path):
    """First sighting of a message is an edit only if we were offline — cache it."""
    logger, client, db = make_logger(tmp_path)
    await logger.on_message_edited(fake_event(text="new"))
    assert db.find_message(-1001, 5) is not None
    assert client.sent == []
    db.close()


async def test_edit_in_ignored_chat_does_nothing(tmp_path):
    logger, client, db = make_logger(tmp_path, ignore={777, -1001})
    seed(db, text="old")
    await logger.on_message_edited(fake_event(text="new"))
    assert client.sent == []
    assert db.get_edits(-1001, 5) == []
    db.close()


async def test_edit_with_identical_text_is_ignored(tmp_path):
    """Telegram fires MessageEdited for reactions and link-preview loads too."""
    logger, client, db = make_logger(tmp_path)
    seed(db, text="same")
    await logger.on_message_edited(fake_event(text="same"))
    assert client.sent == []
    assert db.get_edits(-1001, 5) == []
    db.close()


async def test_edit_records_edit_date_not_send_date(tmp_path):
    """Telegram's Message.date is the original send time and never changes on
    edit — the edit timestamp lives in the separate edit_date field."""
    logger, _client, db = make_logger(tmp_path)
    seed(db, text="old")
    await logger.on_message_edited(
        fake_event(text="new", date_ts=1700000000, edit_date_ts=1700005000)
    )
    edits = db.get_edits(-1001, 5)
    assert edits == [("old", "new", 1700005000)]
    db.close()
