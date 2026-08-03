import pytest

import handlers
from storage import StoredMessage
from tests.test_handlers_new import make_logger

pytestmark = pytest.mark.asyncio

NOW = 1700000000


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(handlers, "RECONCILE_PAUSE_SEC", 0)


def seed(db, *, chat_id, msg_id, text="hi", date=NOW, deleted=False):
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=True,
            chat_title="My Chat",
            sender_id=42,
            sender_name="Alice",
            text=text,
            media_path=None,
            media_type=None,
            date=date,
            deleted=deleted,
        )
    )


def fetcher(client, present_ids):
    async def get_messages(_entity, ids=None):
        return [object() if i in present_ids else None for i in ids]

    client.get_messages = get_messages


async def test_reports_message_missing_from_telegram(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(db, chat_id=-1001, msg_id=5, text="vanished")
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "vanished" in client.sent[0][1]
    assert db.find_message(-1001, 5).deleted is True
    db.close()


async def test_keeps_messages_that_still_exist(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(db, chat_id=-1001, msg_id=5)
    fetcher(client, present_ids={5})

    assert await logger.reconcile(NOW - 1) == 0
    assert client.sent == []
    assert db.find_message(-1001, 5).deleted is False
    db.close()


async def test_skips_rows_older_than_the_window(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(db, chat_id=-1001, msg_id=5, date=NOW - 100)
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW) == 0
    assert client.sent == []
    db.close()


async def test_already_reported_rows_are_not_reported_again(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(db, chat_id=-1001, msg_id=5, deleted=True)
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 0
    assert client.sent == []
    db.close()


async def test_report_budget_defers_the_rest_to_the_next_pass(tmp_path, monkeypatch):
    """A long blind spot can hide hundreds of deletions; draining them in one
    pass would trip FloodWait on the log target."""
    monkeypatch.setattr(handlers, "RECONCILE_MAX_REPORTS", 2)
    logger, client, db = make_logger(tmp_path, watch={-1001})
    for msg_id in (1, 2, 3, 4):
        seed(db, chat_id=-1001, msg_id=msg_id)
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 2
    assert len(client.sent) == 2
    assert await logger.reconcile(NOW - 1) == 2
    assert len(client.sent) == 4
    db.close()


async def test_fetch_failure_does_not_abort_other_chats(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001, -1002})
    seed(db, chat_id=-1001, msg_id=5)
    seed(db, chat_id=-1002, msg_id=6, text="reachable")

    async def flaky_get_messages(entity, ids=None):
        if entity == -1001:
            raise RuntimeError("boom")
        return [None for _ in ids]

    client.get_messages = flaky_get_messages

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "reachable" in client.sent[0][1]
    db.close()


async def test_ignored_chats_are_never_swept(tmp_path):
    logger, client, db = make_logger(tmp_path, ignore={777, -1001})
    seed(db, chat_id=-1001, msg_id=5)
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 0
    assert client.sent == []
    db.close()


async def test_empty_watch_list_sweeps_cached_chats(tmp_path):
    """No watch list means every chat is tracked, so the cache itself is the
    only list of chats worth sweeping."""
    logger, client, db = make_logger(tmp_path)
    seed(db, chat_id=-1001, msg_id=5, text="vanished")
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 1
    assert "vanished" in client.sent[0][1]
    db.close()


async def test_failed_send_leaves_row_for_the_next_pass(tmp_path):
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(db, chat_id=-1001, msg_id=5)
    fetcher(client, present_ids=set())

    async def failing_send_message(*_a, **_kw):
        raise RuntimeError("boom")

    client.send_message = failing_send_message

    assert await logger.reconcile(NOW - 1) == 0
    assert db.find_message(-1001, 5).deleted is False
    db.close()
