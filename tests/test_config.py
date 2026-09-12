from pathlib import Path

import pytest

from config import Config, ConfigError, load_account_configs, load_config, parse_chat_list


def test_parse_chat_list_empty():
    assert parse_chat_list("") == ()
    assert parse_chat_list("   ") == ()


def test_parse_chat_list_strips_and_splits():
    assert parse_chat_list(" @foo, -1001234 ,bar ") == ("@foo", "-1001234", "bar")


def test_parse_chat_list_drops_empty_items():
    assert parse_chat_list("@foo,,@bar,") == ("@foo", "@bar")


def _write_env(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_load_config_reads_values(tmp_path):
    env = _write_env(
        tmp_path,
        "API_ID=123\n"
        "API_HASH=abc\n"
        "SESSION_NAME=sess\n"
        "WATCH_CHATS=@a,@b\n"
        "IGNORE_CHATS=\n"
        "LOG_TARGET=me\n"
        "LOG_OUTGOING=false\n"
        "LOG_EDITS=true\n"
        "DOWNLOAD_MEDIA=true\n"
        "MAX_MEDIA_MB=10\n"
        "MEDIA_DIR=media\n"
        "DB_PATH=messages.db\n"
        "RETENTION_DAYS=7\n"
        "BACKFILL_ON_START=true\n"
        "BACKFILL_LIMIT=50\n"
        "PROXY_ENABLED=false\n",
    )
    cfg = load_config(env)
    assert cfg.api_id == 123
    assert cfg.api_hash == "abc"
    assert cfg.session_name == "sess"
    assert cfg.watch_chats == ("@a", "@b")
    assert cfg.ignore_chats == ()
    assert cfg.log_outgoing is False
    assert cfg.log_edits is True
    assert cfg.retention_days == 7
    assert cfg.backfill_limit == 50


def test_load_config_defaults(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\n")
    cfg = load_config(env)
    assert cfg.session_name == "logger"
    assert cfg.log_target == "me"
    assert cfg.max_media_mb == 50
    assert cfg.retention_days == 14
    assert cfg.backfill_limit == 200
    assert cfg.log_reply_media is True


def test_max_media_bytes(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nMAX_MEDIA_MB=2\n")
    assert load_config(env).max_media_bytes == 2 * 1024 * 1024


def test_missing_api_id_raises(tmp_path):
    env = _write_env(tmp_path, "API_HASH=h\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "API_ID" in str(exc.value)


def test_non_numeric_api_id_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=nope\nAPI_HASH=h\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "API_ID" in str(exc.value)


def test_proxy_dict_disabled(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nPROXY_ENABLED=false\n")
    assert load_config(env).proxy_dict() is None


def test_proxy_dict_enabled(tmp_path):
    env = _write_env(
        tmp_path,
        "API_ID=1\nAPI_HASH=h\n"
        "PROXY_ENABLED=true\nPROXY_TYPE=socks5\n"
        "PROXY_HOST=1.2.3.4\nPROXY_PORT=1080\n",
    )
    assert load_config(env).proxy_dict() == {
        "proxy_type": "socks5",
        "addr": "1.2.3.4",
        "port": 1080,
    }


def test_api_id_zero_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=0\nAPI_HASH=h\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "API_ID" in str(exc.value)


def test_api_id_negative_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=-1\nAPI_HASH=h\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "API_ID" in str(exc.value)


def test_max_media_mb_negative_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nMAX_MEDIA_MB=-1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "MAX_MEDIA_MB" in str(exc.value)


def test_retention_days_zero_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nRETENTION_DAYS=0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "RETENTION_DAYS" in str(exc.value)


def test_backfill_limit_negative_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nBACKFILL_LIMIT=-1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "BACKFILL_LIMIT" in str(exc.value)


def test_proxy_port_zero_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nPROXY_PORT=0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "PROXY_PORT" in str(exc.value)


def test_proxy_port_too_large_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nPROXY_PORT=70000\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "PROXY_PORT" in str(exc.value)


def test_log_edits_typo_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_EDITS=ture\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "LOG_EDITS" in str(exc.value)


def test_logging_and_reconcile_defaults(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\n")
    cfg = load_config(env)
    assert cfg.log_level == "INFO"
    assert cfg.log_file is None
    assert cfg.reconcile_interval_min == 10
    # Defaults to RETENTION_DAYS*24h so nothing ages out of the reconcile
    # window before purge, unless the operator explicitly overrides it.
    assert cfg.reconcile_window_hours == cfg.retention_days * 24


def test_reconcile_window_defaults_to_retention_span(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nRETENTION_DAYS=3\n")
    cfg = load_config(env)
    assert cfg.reconcile_window_hours == 72


def test_reconcile_window_shorter_than_retention_raises(tmp_path):
    env = _write_env(
        tmp_path,
        "API_ID=1\nAPI_HASH=h\nRETENTION_DAYS=3\nRECONCILE_WINDOW_HOURS=12\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "RECONCILE_WINDOW_HOURS" in str(exc.value)


def test_reconcile_window_shorter_than_retention_ok_when_reconcile_disabled(tmp_path):
    env = _write_env(
        tmp_path,
        "API_ID=1\nAPI_HASH=h\nRETENTION_DAYS=3\nRECONCILE_WINDOW_HOURS=12\n"
        "RECONCILE_INTERVAL_MIN=0\n",
    )
    cfg = load_config(env)
    assert cfg.reconcile_window_hours == 12


def test_log_joins_defaults_true(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\n")
    cfg = load_config(env)
    assert cfg.log_joins is True


def test_log_joins_false(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_JOINS=false\n")
    cfg = load_config(env)
    assert cfg.log_joins is False


def test_log_joins_typo_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_JOINS=ture\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "LOG_JOINS" in str(exc.value)


def test_log_reply_media_defaults_true(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\n")
    cfg = load_config(env)
    assert cfg.log_reply_media is True


def test_log_reply_media_false(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_REPLY_MEDIA=false\n")
    cfg = load_config(env)
    assert cfg.log_reply_media is False


def test_log_reply_media_typo_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_REPLY_MEDIA=ture\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "LOG_REPLY_MEDIA" in str(exc.value)


def test_log_level_is_normalised(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_LEVEL=debug\n")
    assert load_config(env).log_level == "DEBUG"


def test_unknown_log_level_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_LEVEL=verbose\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "LOG_LEVEL" in str(exc.value)


def test_log_file_becomes_a_path(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nLOG_FILE=logs/bot.log\n")
    assert load_config(env).log_file == Path("logs/bot.log")


def test_reconcile_interval_zero_disables(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nRECONCILE_INTERVAL_MIN=0\n")
    assert load_config(env).reconcile_interval_min == 0


def test_negative_reconcile_interval_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nRECONCILE_INTERVAL_MIN=-1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "RECONCILE_INTERVAL_MIN" in str(exc.value)


def test_reconcile_window_zero_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nRECONCILE_WINDOW_HOURS=0\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "RECONCILE_WINDOW_HOURS" in str(exc.value)


def test_load_account_configs_falls_back_to_single_account(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNTS_ENV", raising=False)
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nSESSION_NAME=solo\n")
    accounts = load_account_configs(default_env_path=env)
    assert len(accounts) == 1
    label, cfg = accounts[0]
    assert label == "solo"
    assert cfg.api_id == 1


def test_load_account_configs_reads_multiple_files(tmp_path, monkeypatch):
    env1 = tmp_path / "acc1.env"
    env1.write_text(
        "API_ID=1\nAPI_HASH=h1\nSESSION_NAME=one\nDB_PATH=one.db\n", encoding="utf-8"
    )
    env2 = tmp_path / "acc2.env"
    env2.write_text(
        "API_ID=2\nAPI_HASH=h2\nSESSION_NAME=two\nDB_PATH=two.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{env2}")
    accounts = load_account_configs()
    assert [label for label, _cfg in accounts] == ["acc1", "acc2"]
    assert accounts[0][1].api_id == 1
    assert accounts[1][1].api_id == 2


def test_load_account_configs_drops_blank_entries(tmp_path, monkeypatch):
    env1 = tmp_path / "acc1.env"
    env1.write_text("API_ID=1\nAPI_HASH=h1\n", encoding="utf-8")
    monkeypatch.setenv("ACCOUNTS_ENV", f" ,{env1}, ,")
    accounts = load_account_configs()
    assert len(accounts) == 1
    assert accounts[0][0] == "acc1"


def test_load_account_configs_propagates_config_error(tmp_path, monkeypatch):
    bad = tmp_path / "bad.env"
    bad.write_text("API_ID=1\n", encoding="utf-8")  # missing API_HASH
    monkeypatch.setenv("ACCOUNTS_ENV", str(bad))
    with pytest.raises(ConfigError):
        load_account_configs()


def test_load_account_configs_missing_file_raises(tmp_path, monkeypatch):
    """A typo'd path in ACCOUNTS_ENV must not fall through to dotenv_values()
    returning nothing and load_config() silently building a config purely
    from OS environment variables (a silent clone of another account)."""
    env1 = tmp_path / "acc1.env"
    env1.write_text("API_ID=1\nAPI_HASH=h1\n", encoding="utf-8")
    missing = tmp_path / "typo.env"
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{missing}")
    with pytest.raises(ConfigError) as exc:
        load_account_configs()
    assert str(missing) in str(exc.value)
    assert "не найден" in str(exc.value)


def test_load_account_configs_rejects_duplicate_label(tmp_path, monkeypatch):
    """Two files with the same stem produce the same label — used for
    per-account logging — so this must be rejected too."""
    dir1 = tmp_path / "d1"
    dir2 = tmp_path / "d2"
    dir1.mkdir()
    dir2.mkdir()
    env1 = dir1 / "acc.env"
    env1.write_text("API_ID=1\nAPI_HASH=h1\nSESSION_NAME=one\nDB_PATH=one.db\n",
                     encoding="utf-8")
    env2 = dir2 / "acc.env"
    env2.write_text("API_ID=2\nAPI_HASH=h2\nSESSION_NAME=two\nDB_PATH=two.db\n",
                     encoding="utf-8")
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{env2}")
    with pytest.raises(ConfigError) as exc:
        load_account_configs()
    assert "label" in str(exc.value)


def test_load_account_configs_rejects_duplicate_session_name(tmp_path, monkeypatch):
    """Two accounts sharing a session_name would collapse into one shared
    Telethon session with zero warning — this must be caught up front."""
    env1 = tmp_path / "acc1.env"
    env1.write_text(
        "API_ID=1\nAPI_HASH=h1\nSESSION_NAME=shared\nDB_PATH=one.db\n", encoding="utf-8"
    )
    env2 = tmp_path / "acc2.env"
    env2.write_text(
        "API_ID=2\nAPI_HASH=h2\nSESSION_NAME=shared\nDB_PATH=two.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{env2}")
    with pytest.raises(ConfigError) as exc:
        load_account_configs()
    assert "session_name" in str(exc.value)


def test_load_account_configs_rejects_duplicate_db_path(tmp_path, monkeypatch):
    """Two accounts sharing a db_path would collapse into one shared SQLite DB
    (including a shared watch_chats table) with zero warning."""
    env1 = tmp_path / "acc1.env"
    env1.write_text(
        "API_ID=1\nAPI_HASH=h1\nSESSION_NAME=one\nDB_PATH=shared.db\n", encoding="utf-8"
    )
    env2 = tmp_path / "acc2.env"
    env2.write_text(
        "API_ID=2\nAPI_HASH=h2\nSESSION_NAME=two\nDB_PATH=shared.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{env2}")
    with pytest.raises(ConfigError) as exc:
        load_account_configs()
    assert "db_path" in str(exc.value)


def test_max_media_total_mb_defaults_to_zero(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\n")
    cfg = load_config(env)
    assert cfg.max_media_total_mb == 0
    assert cfg.max_media_total_bytes == 0


def test_max_media_total_mb_reads_value(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nMAX_MEDIA_TOTAL_MB=10\n")
    cfg = load_config(env)
    assert cfg.max_media_total_mb == 10
    assert cfg.max_media_total_bytes == 10 * 1024 * 1024


def test_max_media_total_mb_negative_raises(tmp_path):
    env = _write_env(tmp_path, "API_ID=1\nAPI_HASH=h\nMAX_MEDIA_TOTAL_MB=-1\n")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "MAX_MEDIA_TOTAL_MB" in str(exc.value)


def test_load_account_configs_allows_distinct_accounts(tmp_path, monkeypatch):
    """Sanity check: accounts with distinct label/session_name/db_path must
    load fine — the collision guard must not be over-eager."""
    env1 = tmp_path / "acc1.env"
    env1.write_text(
        "API_ID=1\nAPI_HASH=h1\nSESSION_NAME=one\nDB_PATH=one.db\n", encoding="utf-8"
    )
    env2 = tmp_path / "acc2.env"
    env2.write_text(
        "API_ID=2\nAPI_HASH=h2\nSESSION_NAME=two\nDB_PATH=two.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("ACCOUNTS_ENV", f"{env1},{env2}")
    accounts = load_account_configs()
    assert [label for label, _cfg in accounts] == ["acc1", "acc2"]
