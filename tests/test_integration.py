import pytest

from tests.test_handlers_new import fake_event, make_logger
from tests.test_handlers_delete import delete_event

pytestmark = pytest.mark.asyncio


async def test_new_message_then_deleted_in_dm_end_to_end(tmp_path):
    """Real on_new_message -> on_message_deleted round trip for a DM, not a
    hand-seeded row — proves chat_id/is_channel agree between write and lookup
    for the branch where MessageDeleted carries no chat_id."""
    logger, client, db = make_logger(tmp_path)
    await logger.on_new_message(
        fake_event(chat_id=555, msg_id=42, is_channel=False, text="secret dm")
    )
    await logger.on_message_deleted(delete_event([42], chat_id=None))
    assert len(client.sent) == 1
    assert "secret dm" in client.sent[0][1]
    db.close()


async def test_new_message_then_deleted_in_channel_end_to_end(tmp_path):
    """Same round trip for a channel/supergroup, where MessageDeleted DOES
    carry chat_id."""
    logger, client, db = make_logger(tmp_path)
    await logger.on_new_message(
        fake_event(chat_id=-1001, msg_id=7, is_channel=True, text="channel post")
    )
    await logger.on_message_deleted(delete_event([7], chat_id=-1001))
    assert len(client.sent) == 1
    assert "channel post" in client.sent[0][1]
    db.close()
