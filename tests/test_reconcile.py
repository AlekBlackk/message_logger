import pytest

import handlers
from storage import StoredMessage
from tests.test_handlers_new import make_logger

pytestmark = pytest.mark.asyncio

NOW = 1700000000


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(handlers, "RECONCILE_PAUSE_SEC", 0)


def seed(
    db,
    *,
    chat_id,
    msg_id,
    text="hi",
    date=NOW,
    deleted=False,
    sender_id=42,
    reply_to_msg_id=None,
):
    db.save_message(
        StoredMessage(
            chat_id=chat_id,
            msg_id=msg_id,
            is_channel=True,
            chat_title="My Chat",
            sender_id=sender_id,
            sender_name="Alice",
            text=text,
            media_path=None,
            media_type=None,
            date=date,
            deleted=deleted,
            reply_to_msg_id=reply_to_msg_id,
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


async def test_reconcile_suppresses_collateral_reply_when_parent_recently_deleted(tmp_path):
    """When reconcile discovers a missing reply whose parent was recently deleted
    (e.g. in live stream) with a different sender, it is marked deleted silently without sending a report."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="banned user post",
        sender_id=99,
        deleted=True,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="collateral reply",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    logger.record_deletion(-1001, 1)
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 0
    assert client.sent == []
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_reconcile_reports_reply_when_parent_deleted_long_ago(tmp_path):
    """When reconcile discovers a missing reply whose parent was deleted in the past
    (not part of recent cascade or this reconcile pass), it is reported as an intentional deletion."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="old deleted parent",
        sender_id=99,
        deleted=True,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="my reply",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "my reply" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_reconcile_reports_banned_message_and_suppresses_reply_in_same_pass(tmp_path):
    """When reconcile discovers both parent and reply missing in the same pass,
    it reports the parent and suppresses the collateral reply."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="banned user post",
        sender_id=99,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="collateral reply",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "banned user post" in client.sent[0][1]
    assert db.find_message(-1001, 1).deleted is True
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_reconcile_reports_manual_reply_deletion_when_parent_still_exists(tmp_path):
    """If the parent message still exists in Telegram, a missing reply is an
    intentional deletion and must be reported."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="living parent",
        sender_id=99,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="deleted reply",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    fetcher(client, present_ids={1})

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "deleted reply" in client.sent[0][1]
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_reconcile_reports_banned_user_self_replies(tmp_path):
    """A banned user's replies to their own earlier messages are reported,
    not suppressed as collateral."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="banned post 1",
        sender_id=99,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="banned post 2 (reply to self)",
        sender_id=99,
        reply_to_msg_id=1,
        deleted=False,
    )
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 2
    assert len(client.sent) == 2
    assert db.find_message(-1001, 1).deleted is True
    assert db.find_message(-1001, 2).deleted is True
    db.close()


async def test_reconcile_suppresses_multi_level_reply_chain(tmp_path):
    """In reconcile, a banned user's post has a reply from user B, which in turn
    has a reply from user C. Both replies are collateral and must be suppressed."""
    logger, client, db = make_logger(tmp_path, watch={-1001})
    seed(
        db,
        chat_id=-1001,
        msg_id=1,
        text="banned user root",
        sender_id=99,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=2,
        text="reply by bob",
        sender_id=42,
        reply_to_msg_id=1,
        deleted=False,
    )
    seed(
        db,
        chat_id=-1001,
        msg_id=3,
        text="reply by charlie to bob",
        sender_id=43,
        reply_to_msg_id=2,
        deleted=False,
    )
    fetcher(client, present_ids=set())

    assert await logger.reconcile(NOW - 1) == 1
    assert len(client.sent) == 1
    assert "banned user root" in client.sent[0][1]
    assert db.find_message(-1001, 1).deleted is True
    assert db.find_message(-1001, 2).deleted is True
    assert db.find_message(-1001, 3).deleted is True
    db.close()

