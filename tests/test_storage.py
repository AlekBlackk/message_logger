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


def test_overwrite_never_resurrects_a_deleted_row(tmp_path):
    """catch_up replays NewMessage updates after a reconnect. A replay for a
    message already reported deleted must not clear the flag — reconcile would
    otherwise see it alive again and report the same deletion a second time."""
    db = open_db(tmp_path)
    db.save_message(make_msg(text="original", media_path="media/10.jpg", media_size=99))
    db.mark_deleted(-1001, 10)
    db.save_message(make_msg(text="replayed"))
    got = db.find_message(-1001, 10)
    assert got.deleted is True
    assert got.media_path == "media/10.jpg"
    assert got.media_size == 99
    assert db.alive_ids(-1001, 0) == []
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

    rows_purged, paths = db.purge_older_than(300)

    assert rows_purged == 2
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
    """cutoff_ts is a lower bound: /purge <days> targets the last N days
    (date >= cutoff), not rows older than it."""
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="media/1.jpg"))
    db.save_message(make_msg(msg_id=2, date=100, media_path=None))
    db.save_message(make_msg(msg_id=3, date=500))
    total, media = db.count_purge_candidates(300, None)
    assert (total, media) == (1, 0)
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
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))  # before cutoff, excluded
    db.save_message(make_msg(chat_id=-1001, msg_id=2, date=500))  # within last N days
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=500))  # right chat, wrong id
    total, media = db.count_purge_candidates(300, -1001)
    assert (total, media) == (1, 0)
    db.close()


def test_count_purge_candidates_zero_match(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100))  # before cutoff, excluded
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
    db.save_message(make_msg(chat_id=-1001, msg_id=1, date=100))  # before cutoff, kept
    db.save_message(make_msg(chat_id=-1001, msg_id=2, date=500))  # within last N days, purged
    db.save_message(make_msg(chat_id=-1002, msg_id=1, date=500))  # right age, wrong chat, kept

    db.purge(300, -1001)

    assert db.find_message(-1001, 1) is not None
    assert db.find_message(-1001, 2) is None
    assert db.find_message(-1002, 1) is not None
    db.close()


def test_purge_requires_a_filter(tmp_path):
    db = open_db(tmp_path)
    with pytest.raises(ValueError):
        db.purge(None, None)
    db.close()


def test_migrate_adds_columns_to_pre_existing_db(tmp_path):
    """A DB created before media_size/reply_to_msg_id existed must gain them
    via ALTER TABLE, not silently keep the old schema (CREATE TABLE IF NOT
    EXISTS is a no-op on a table that already exists)."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages (
            chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
            is_channel INTEGER NOT NULL DEFAULT 0, chat_title TEXT,
            sender_id INTEGER, sender_name TEXT, text TEXT,
            media_path TEXT, media_type TEXT, date INTEGER NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, msg_id)
        );
        CREATE TABLE watch_chats (chat_id INTEGER PRIMARY KEY, label TEXT, added_at INTEGER);
        """
    )
    conn.commit()
    conn.close()

    db = Storage(path)
    db.save_message(make_msg())
    got = db.find_message(-1001, 10)
    assert got.media_size is None
    assert got.reply_to_msg_id is None
    db.close()


def test_media_size_and_reply_to_round_trip(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(media_size=123, reply_to_msg_id=9))
    got = db.find_message(-1001, 10)
    assert got.media_size == 123
    assert got.reply_to_msg_id == 9
    db.close()


def test_media_total_bytes_sums_only_rows_with_media(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, media_path="a.jpg", media_size=100))
    db.save_message(make_msg(msg_id=2, media_path="b.jpg", media_size=200))
    db.save_message(make_msg(msg_id=3, media_path=None, media_size=None))
    assert db.media_total_bytes() == 300
    db.close()


def test_evict_oldest_media_stops_once_under_target(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="a.jpg", media_size=100))
    db.save_message(make_msg(msg_id=2, date=200, media_path="b.jpg", media_size=100))
    db.save_message(make_msg(msg_id=3, date=300, media_path="c.jpg", media_size=100))

    # total=300 over target=250: evicting only the oldest (a.jpg, 100) leaves
    # 200, already <=250, so eviction stops there without touching b.jpg.
    paths = db.evict_oldest_media(250)

    assert paths == ["a.jpg"]
    assert db.find_message(-1001, 1).media_path is None
    assert db.find_message(-1001, 1).media_size is None
    assert db.find_message(-1001, 2).media_path == "b.jpg"
    assert db.media_total_bytes() == 200
    db.close()


def test_evict_oldest_media_noop_when_already_under_target(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="a.jpg", media_size=50))
    assert db.evict_oldest_media(1000) == []
    assert db.find_message(-1001, 1).media_path == "a.jpg"
    db.close()


def test_evict_oldest_media_keeps_message_row(tmp_path):
    """Eviction drops the media reference only — text and metadata survive."""
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, date=100, media_path="a.jpg", media_size=100, text="hi"))
    db.evict_oldest_media(0)
    got = db.find_message(-1001, 1)
    assert got is not None
    assert got.text == "hi"
    assert got.media_path is None
    db.close()


def test_rows_missing_media_size(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, media_path="a.jpg", media_size=None))
    db.save_message(make_msg(msg_id=2, media_path="b.jpg", media_size=50))
    db.save_message(make_msg(msg_id=3, media_path=None))
    assert db.rows_missing_media_size() == [(-1001, 1, "a.jpg")]
    db.close()


def test_set_media_size(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(msg_id=1, media_path="a.jpg", media_size=None))
    db.set_media_size(-1001, 1, 77)
    assert db.find_message(-1001, 1).media_size == 77
    db.close()


def test_stats_counts_and_top_chats(tmp_path):
    db = open_db(tmp_path)
    db.save_message(make_msg(chat_id=-1, msg_id=1, date=100))
    db.save_message(make_msg(chat_id=-1, msg_id=2, date=200))
    db.save_message(make_msg(chat_id=-2, msg_id=1, date=150))
    db.mark_deleted(-1, 1)
    db.add_edit(-1, 2, "old", "new", 200)
    db.add_watch(-1, "Chat 1", 1000)

    stats = db.stats()

    assert stats["total_messages"] == 3
    assert stats["deleted_messages"] == 1
    assert stats["total_edits"] == 1
    assert stats["distinct_chats"] == 2
    assert stats["watched_chats"] == 1
    assert stats["oldest_date"] == 100
    assert stats["newest_date"] == 200
    assert stats["top_chats"][0] == (-1, 2)
    db.close()


def test_stats_empty_db(tmp_path):
    db = open_db(tmp_path)
    stats = db.stats()
    assert stats["total_messages"] == 0
    assert stats["oldest_date"] is None
    assert stats["newest_date"] is None
    assert stats["top_chats"] == []
    db.close()


def test_report_message_ids_roundtrip_and_forget(tmp_path):
    db = open_db(tmp_path)
    db.add_report_message(10)
    db.add_report_message(11)
    db.add_report_message(10)  # re-adding is harmless
    assert sorted(db.report_message_ids()) == [10, 11]
    db.forget_report_messages([10])
    assert db.report_message_ids() == [11]
    db.close()
