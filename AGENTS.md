# AGENTS.md

Инструкции для агентов, работающих в этом репозитории.

## Что это

Telegram-юзербот на Telethon (MTProto). Кеширует входящие сообщения в SQLite,
при удалении/редактировании присылает отчёт в `LOG_TARGET` (по умолчанию Избранное)
вместе с сохранённым медиа. Личный аккаунт, личное использование.

## Структура

| Файл | Ответственность |
| --- | --- |
| `main.py` | Точка входа: `run_all()` поднимает `run_account()` для каждого аккаунта из `load_account_configs()` через `asyncio.gather` — сборка клиента, регистрация хендлеров, backfill, цикл ретеншена, graceful shutdown на аккаунт, падение одного не валит остальные. |
| `config.py` | `Config` (frozen dataclass), `load_config()`, парсинг и валидация `.env`. |
| `storage.py` | SQLite-слой: схема (`messages`, `edits`), `StoredMessage`, вставка/чтение/очистка. |
| `handlers.py` | `MessageLogger`: обработчики `NewMessage`, `MessageDeleted`, `MessageEdited`, кеш имён, сверка кеша с Telegram (`reconcile`). |
| `reports.py` | Чистое форматирование текста отчётов (`format_deleted`, `format_edited`). |
| `tests/` | pytest, `asyncio_mode = auto`. |

Зависимость направлена в одну сторону: `main` → `handlers` → (`storage`, `reports`, `config`).
`reports.py` не знает о Telethon и остаётся чистым — новую логику форматирования класть туда.

## Команды

Установка:

```bash
pip install -r requirements-dev.txt
```

Тесты:

```bash
python -m pytest
```

Запуск (интерактивный вход, создаёт файл сессии):

```bash
python main.py
```

## Правила

- Python 3.10+. `from __future__ import annotations` в каждом модуле, аннотации типов везде.
- Комментарий пишем только там, где код неочевиден, и объясняем *почему*, а не *что*.
  Пример из `main.py`: зачем `utils.get_peer_id` вместо `entity.id`.
- Id чатов нормализуем через `telethon.utils.get_peer_id`. Сырой `entity.id` для
  супергрупп не совпадёт с id из апдейта (`-100…`).
- Новый параметр конфигурации: добавить поле в `Config`, разбор в `load_config`,
  строку в `.env.example`, строку в таблицу README и тест в `tests/test_config.py`.
- Ошибки конфигурации кидаем `ConfigError`, не `SystemExit` из библиотечного кода.
- Событие удаления даёт только id сообщений; вся остальная информация берётся из кеша.
  Не полагаться на то, что Telegram что-то ещё пришлёт.
- Тесты не ходят в сеть. Telethon-клиент мокается.

## Не трогать / не коммитить

`*.session`, `*.db*`, `.env`, `media/` — покрыты `.gitignore`.
Файл сессии даёт полный доступ к аккаунту: не копировать, не выводить в логи,
не прикладывать к отчётам. Реальные `API_ID` / `API_HASH` в примеры не подставлять.

## Git

- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
- Без трейлера `Co-Authored-By`.
- Основная ветка — `main`.
