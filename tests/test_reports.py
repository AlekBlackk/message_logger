from reports import MAX_TEXT_LEN, format_deleted, format_edited, format_membership, truncate
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


def test_format_membership_escapes_html():
    out = make_membership(user_name="<script>", chat_title="a&b")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "a&amp;b" in out
