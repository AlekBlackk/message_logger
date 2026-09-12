from reports import (
    MAX_TEXT_LEN,
    format_deleted,
    format_edited,
    format_membership,
    format_reply_media_caption,
    format_stats,
    truncate,
)
from storage import StoredMessage


def make_msg(**kw):
    base = dict(
        chat_id=-1001,
        msg_id=10,
        is_channel=True,
        chat_title="My Chat",
        sender_id=42,
        sender_name="Alice",
        text="hello",
        media_path=None,
        media_type=None,
        date=1700000000,
        deleted=False,
    )
    base.update(kw)
    return StoredMessage(**base)


def test_truncate_short_text_unchanged():
    assert truncate("hi") == "hi"


def test_truncate_none_is_empty():
    assert truncate(None) == ""


def test_truncate_long_text():
    out = truncate("x" * (MAX_TEXT_LEN + 100))
    assert len(out) <= MAX_TEXT_LEN + 1
    assert out.endswith("…")


def test_format_deleted_contains_fields():
    out = format_deleted(make_msg())
    assert "My Chat" in out
    assert "Alice" in out
    assert "42" in out
    assert "hello" in out
    assert "10" in out


def test_format_deleted_escapes_html():
    out = format_deleted(make_msg(text="<b>bold</b> & stuff"))
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; stuff" in out
    assert "<b>bold</b>" not in out


def test_format_deleted_escapes_names():
    out = format_deleted(make_msg(sender_name="<script>", chat_title="a&b"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "a&amp;b" in out


def test_format_deleted_empty_text_has_placeholder():
    out = format_deleted(make_msg(text=None, media_type="photo"))
    assert "photo" in out


def test_format_deleted_truncates():
    out = format_deleted(make_msg(text="y" * (MAX_TEXT_LEN + 500)))
    assert len(out) < MAX_TEXT_LEN + 800


def test_format_edited_shows_both_versions():
    out = format_edited(make_msg(), "before", "after")
    assert "before" in out
    assert "after" in out
    assert "My Chat" in out


def test_format_edited_escapes_html():
    out = format_edited(make_msg(), "<i>a</i>", "<i>b</i>")
    assert "<i>a</i>" not in out
    assert "&lt;i&gt;a&lt;/i&gt;" in out


def make_membership(**kw):
    base = dict(
        joined=True,
        chat_title="My Chat",
        chat_id=-1001,
        chat_username=None,
        user_name="Bob",
        user_id=99,
        user_username="bobby",
    )
    base.update(kw)
    return format_membership(**base)


def test_format_membership_joined_contains_fields():
    out = make_membership()
    assert "Вступление" in out
    assert "My Chat" in out
    assert "Bob" in out
    assert "99" in out


def test_format_membership_left():
    out = make_membership(joined=False)
    assert "Выход" in out


def test_format_membership_with_actor():
    out = make_membership(actor_name="Carl", actor_id=7, actor_username="carl")
    assert "Carl" in out
    assert "Добавил" in out


def test_format_membership_left_with_actor_says_kicked():
    out = make_membership(joined=False, actor_name="Dana", actor_id=8)
    assert "Удалил" in out


def test_format_membership_without_actor_omits_line():
    out = make_membership()
    assert "Добавил" not in out
    assert "Удалил" not in out


def test_format_deleted_includes_reply_preview():
    out = format_deleted(make_msg(), reply_preview="Bob: original text")
    assert "В ответ на" in out
    assert "Bob: original text" in out


def test_format_deleted_omits_reply_line_when_none():
    out = format_deleted(make_msg())
    assert "В ответ на" not in out


def test_format_deleted_escapes_reply_preview():
    out = format_deleted(make_msg(), reply_preview="<script>x</script>")
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;" in out


def test_format_edited_includes_reply_preview():
    out = format_edited(make_msg(), "before", "after", reply_preview="Bob: original")
    assert "В ответ на" in out
    assert "Bob: original" in out


def test_format_stats_contains_counts():
    out = format_stats(
        total_messages=10,
        deleted_messages=2,
        total_edits=3,
        distinct_chats=4,
        watched_chats=1,
        oldest_date=1700000000,
        newest_date=1700003600,
        db_size_bytes=2048,
        media_file_count=5,
        media_dir_bytes=1024 * 1024,
        top_chats=[("Chat A", 7), ("Chat B", 3)],
    )
    assert "10" in out
    assert "Chat A" in out
    assert "7" in out
    assert "2.0 КБ" in out
    assert "1.0 МБ" in out


def test_format_stats_omits_date_range_when_empty():
    out = format_stats(
        total_messages=0,
        deleted_messages=0,
        total_edits=0,
        distinct_chats=0,
        watched_chats=0,
        oldest_date=None,
        newest_date=None,
        db_size_bytes=0,
        media_file_count=0,
        media_dir_bytes=0,
        top_chats=[],
    )
    assert "Диапазон дат" not in out


def test_format_stats_escapes_chat_labels():
    out = format_stats(
        total_messages=1,
        deleted_messages=0,
        total_edits=0,
        distinct_chats=1,
        watched_chats=0,
        oldest_date=None,
        newest_date=None,
        db_size_bytes=0,
        media_file_count=0,
        media_dir_bytes=0,
        top_chats=[("<script>", 1)],
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_membership_escapes_html():
    out = make_membership(user_name="<script>", chat_title="a&b")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "a&amp;b" in out


def test_format_reply_media_caption():
    assert format_reply_media_caption("фото", "Alice") == "↩️ В ответ на фото от Alice"
    assert format_reply_media_caption("видео", "Bob") == "↩️ В ответ на видео от Bob"
    assert format_reply_media_caption("гиф", "Charlie") == "↩️ В ответ на гиф от Charlie"
    assert format_reply_media_caption("animation", "Charlie") == "↩️ В ответ на гиф от Charlie"
    assert format_reply_media_caption("голос", "David") == "↩️ В ответ на голос от David"
    assert format_reply_media_caption("голосовое", "Eve") == "↩️ В ответ на голос от Eve"
    assert format_reply_media_caption("файл", "Frank") == "↩️ В ответ на файл от Frank"
    assert format_reply_media_caption("документ", "Frank") == "↩️ В ответ на файл от Frank"
    assert format_reply_media_caption("video_note", "Grace") is None
    assert format_reply_media_caption("  video_note  ", "Grace") is None
    assert format_reply_media_caption("видеосообщение", "Grace") is None
    assert format_reply_media_caption("кружочек", "Grace") is None
    assert format_reply_media_caption("sticker", "Heidi") is None
    assert format_reply_media_caption("стикер", "Heidi") is None
    assert format_reply_media_caption(" стикер \n", "Heidi") is None
    assert format_reply_media_caption(None, "Ivan") == "↩️ В ответ на медиа от Ivan"
    assert format_reply_media_caption("фото", None) == "↩️ В ответ на фото от ?"
    assert format_reply_media_caption("фото", "   ") == "↩️ В ответ на фото от ?"
    assert (
        format_reply_media_caption("фото", "<b>Alice</b>")
        == "↩️ В ответ на фото от &lt;b&gt;Alice&lt;/b&gt;"
    )

