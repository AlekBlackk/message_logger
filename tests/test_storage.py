import pytest

from storage import StoredMessage, Storage


def make_msg(**kw):
    base = dict(
        chat_id=-1001,
        msg_id=10,
        is_channel=True,
        chat_title="Chat",
        sender_id=42,
        sender_name="Alice",
        text="hello",
        media_path=None,
        media_type=None,
        date=1000,
        deleted=False,
    )
    base.update(kw)
    return StoredMessage(**base)


def open_db(tmp_path):
    return Storage(tmp_path / "test.db")


def test_save_and_find_by_chat_and_id(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg())
    got = db.find_message(-1001, 10)
    assert got is not None
    assert got.text == "hello"
    assert got.is_channel is True
    assert got.deleted is False
    db.close()


def test_find_returns_none_when_absent(tmp_path):
    db = open_db(tmp_path)
    assert db.find_message(-1001, 10) is None
    db.close()


def test_find_without_chat_id_ignores_channel_rows(tmp_path):
    """Deletion events for DMs carry no chat_id and msg_ids collide with channels."""
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=7, is_channel=True, text="channel"))
    db.save_message(make_msg(chat_id=555, msg_id=7, is_channel=False, text="dm"))
    got = db.find_message(None, 7)
    assert got is not None
    assert got.text == "dm"
    db.close()


def test_find_without_chat_id_returns_none_for_channel_only(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=7, is_channel=True))
    assert db.find_message(None, 7) is None
    db.close()


def test_save_overwrites_by_default(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(text="first"))
    db.save_message(make_msg(text="second"))
    assert db.find_message(-1001, 10).text == "second"
    db.close()


def test_save_without_overwrite_keeps_existing_row(tmp_path):
    """Backfill must not clobber rows already flagged deleted."""
    db = open_db(tmp_path)
    db.save_message(make_msg(text="original"))
    db.mark_deleted(-1001, 10)
    db.save_message(make_msg(text="backfilled"), overwrite=False)
    got = db.find_message(-1001, 10)
    assert got.text == "original"
    assert got.deleted is True
    db.close()


def test_mark_deleted(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg())
    db.mark_deleted(-1001, 10)
    assert db.find_message(-1001, 10).deleted is True
    db.close()


def test_update_text(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(text="old"))
    db.update_text(-1001, 10, "new")
    assert db.find_message(-1001, 10).text == "new"
    db.close()


def test_add_and_get_edits(tmp_path):
    db = open_db(tmp_path)
    db.add_edit(-1001, 10, "old", "new", 1234)
    db.add_edit(-1001, 10, "new", "newer", 1235)
    edits = db.get_edits(-1001, 10)
    assert edits == [("old", "new", 1234), ("new", "newer", 1235)]
    db.close()


def test_purge_older_than_removes_rows_and_returns_media(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="media/1.jpg"))
    db.save_message(make_msg(msg_id=2, date=100, media_path=None))
    db.save_message(make_msg(msg_id=3, date=500))
    db.add_edit(-1001, 1, "a", "b", 100)
    db.add_edit(-1001, 3, "a", "b", 500)

    paths = db.purge_older_than(300)

    assert paths == ["media/1.jpg"]
    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1001, 2) is None
    assert db.find_message(-1001, 3) is not None
    assert db.get_edits(-1001, 1) == []
    assert db.get_edits(-1001, 3) != []
    db.close()


def test_purge_removes_edits_dated_after_their_message(tmp_path):
    """Edit rows can be dated later than their message — purge must still catch them."""
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100))
    db.add_edit(-1001, 1, "before", "after", 250)
    db.purge_older_than(200)
    assert db.find_message(-1001, 1) is None
    assert db.get_edits(-1001, 1) == []
    db.close()


def test_wal_mode_enabled(tmp_path):
    db = open_db(tmp_path)
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


def test_add_list_remove_watch(tmp_path):
    db = open_db(tmp_path)
    assert db.list_watch() == []
    db.add_watch(-1001, "Chat A", 1000)
    db.add_watch(-1002, None, 1001)
    assert db.list_watch() == [(-1001, "Chat A", 1000), (-1002, None, 1001)]
    db.remove_watch(-1001)
    assert db.list_watch() == [(-1002, None, 1001)]
    db.close()


def test_add_watch_replaces_existing_row(tmp_path):
    db = open_db(tmp_path)
    db.add_watch(-1001, "old label", 1000)
    db.add_watch(-1001, "new label", 2000)
    assert db.list_watch() == [(-1001, "new label", 2000)]
    db.close()


def test_remove_watch_missing_row_is_a_noop(tmp_path):
    db = open_db(tmp_path)
    db.remove_watch(-1001)  # must not raise
    assert db.list_watch() == []
    db.close()


def test_count_purge_candidates_by_age(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="media/1.jpg"))
    db.save_message(make_msg(msg_id=2, date=100, media_path=None))
    db.save_message(make_msg(msg_id=3, date=500))
    total, media = db.count_purge_candidates(300, None)
    assert (total, media) == (2, 1)
    db.close()


def test_count_purge_candidates_by_chat(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))
    total, media = db.count_purge_candidates(None, -1001)
    assert (total, media) == (1, 0)
    db.close()


def test_count_purge_candidates_by_chat_and_age(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1001, msg_id=2, date=500))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))
    total, media = db.count_purge_candidates(300, -1001)
    assert (total, media) == (1, 0)
    db.close()


def test_count_purge_candidates_zero_match(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=500))
    total, media = db.count_purge_candidates(300, None)
    assert (total, media) == (0, 0)
    db.close()


def test_count_purge_candidates_requires_a_filter(tmp_path):
    db = open_db(tmp_path)
    with pytest.raises(ValueError):
        db.count_purge_candidates(None, None)
    db.close()


def test_purge_by_chat_removes_rows_and_returns_media(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100, media_path="media/1.jpg"))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100, media_path="media/2.jpg"))
    db.add_edit(-1001, 1, "a", "b", 100)

    paths = db.purge(None, -1001)

    assert paths == ["media/1.jpg"]
    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1002, 1) is not None
    assert db.get_edits(-1001, 1) == []
    db.close()


def test_purge_by_chat_and_age_combines_filters(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1001, msg_id=2, date=500))
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=100))

    db.purge(300, -1001)

    assert db.find_message(-1001, 1) is None
    assert db.find_message(-1001, 2) is not None
    assert db.find_message(-1002, 1) is not None
    db.close()


def test_purge_requires_a_filter(tmp_path):
    db = open_db(tmp_path)
    with pytest.raises(ValueError):
        db.purge(None, None)
    db.close()
