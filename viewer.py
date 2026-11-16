"""Web viewer for the message log databases.

Read-only: opens every database with mode=ro, so it can run next to the bot.
Binds to localhost by default — reach it through an SSH tunnel, or set
--host 0.0.0.0 together with VIEWER_TOKEN.

    python3 viewer.py                      # all *.db in the current folder
    python3 viewer.py --db messages.db --db acc2.db --port 8080
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import sqlite3
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
PAGE_SIZE = 50

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Удалённые сообщения</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1b1f24;--muted:#6b7280;--line:#e3e6ea;--accent:#2563eb;--del:#dc2626;--edit:#d97706;--hh:0px}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#181c22;--text:#e6e8eb;--muted:#8b93a0;--line:#262b33;--accent:#60a5fa;--del:#f87171;--edit:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
.bar{max-width:900px;margin:0 auto;display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center}
.bar+.bar{margin-top:8px}
input,select,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 9px}
button{cursor:pointer}
#q{flex:1 1 200px;min-width:0}
select{max-width:200px}
.seg{display:flex}.seg button{border-radius:0}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0;border-left:0}
.on{background:var(--accent)!important;border-color:var(--accent)!important;color:#fff!important}
.chips{display:flex;gap:4px}.chips button{padding:4px 10px;border-radius:99px;font-size:13px}
.dates{display:flex;gap:4px;align-items:center;color:var(--muted)}
.dates input{padding:4px 6px;width:140px}
#count{margin-left:auto;color:var(--muted);font-size:13px}
#reset{color:var(--accent);background:none;border:0;padding:4px}
main{max-width:900px;margin:0 auto;padding:0 16px 64px}
.day{position:sticky;top:var(--hh);z-index:5;background:var(--bg);padding:10px 0 6px;color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:6px}
.meta{display:flex;flex-wrap:wrap;gap:2px 10px;align-items:baseline;font-size:12px;color:var(--muted)}
.meta b{color:var(--text);font-weight:600;font-size:13px}
.badge{font-size:10px;font-weight:700;border-radius:99px;padding:0 7px;border:1px solid currentColor}
.badge.del{color:var(--del)}.badge.edit{color:var(--edit)}
.text{white-space:pre-wrap;word-break:break-word;margin-top:3px;cursor:pointer}
.text.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.media{margin-top:6px;color:var(--muted)}
.media img,.media video{max-width:100%;max-height:260px;border-radius:8px;display:block}
.media a{color:var(--accent)}
.edits{margin-top:6px;border-left:3px solid var(--edit);padding-left:10px;font-size:13px}
.edits .old{color:var(--muted);text-decoration:line-through;white-space:pre-wrap;word-break:break-word}
.edits .new{white-space:pre-wrap;word-break:break-word;margin-bottom:4px}
.end{text-align:center;color:var(--muted);padding:24px 0}
#top{position:fixed;right:18px;bottom:18px;z-index:20;border-radius:99px;padding:8px 14px;box-shadow:0 2px 10px #0004}
</style>
</head>
<body>
<header>
<div class="bar">
  <select id="db" title="Аккаунт"></select>
  <input id="q" type="search" placeholder="Поиск по тексту, автору, чату" autocomplete="off">
  <div class="seg"><button id="k-deleted" class="on">Удалённые</button><button id="k-edited">Изменённые</button></div>
  <button id="refresh" title="Обновить">↻</button>
</div>
<div class="bar">
  <select id="chat" title="Чат"></select>
  <select id="sender" title="Отправитель"></select>
  <select id="media" title="Вложение"></select>
  <div class="chips" id="chips">
    <button data-d="all" class="on">Всё</button><button data-d="0">Сегодня</button><button data-d="1">Вчера</button><button data-d="7">7 дн</button><button data-d="30">30 дн</button>
  </div>
  <div class="dates"><input id="from" type="date" title="С"> — <input id="to" type="date" title="По"></div>
  <button id="reset">Сбросить</button>
  <span id="count"></span>
</div>
</header>
<main><div id="list"></div><div id="sentinel" class="end"></div></main>
<button id="top" hidden>↑ Вверх</button>
<script>
const $ = id => document.getElementById(id);
const state = {db: "", chat: "", sender: "", media: "", kind: "deleted", q: "", from: "", to: "", offset: 0, loading: false, done: false, gen: 0};
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const fmtDay = ts => new Date(ts * 1000).toLocaleDateString("ru-RU", {weekday: "long", day: "numeric", month: "long", year: "numeric"});
const dayKey = ts => new Date(ts * 1000).toDateString();
const isoDay = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");

async function api(path, params) {
  const r = await fetch(path + "?" + new URLSearchParams(params));
  if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
  return r.json();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// "to" is inclusive in the UI, exclusive in the API: add one day.
function range() {
  const out = {};
  if (state.from) out.from = Math.floor(new Date(state.from + "T00:00:00").getTime() / 1000);
  if (state.to) out.to = Math.floor(new Date(state.to + "T00:00:00").getTime() / 1000) + 86400;
  return out;
}

function filters() {
  return {db: state.db, kind: state.kind, chat: state.chat, sender: state.sender, media: state.media, q: state.q, ...range()};
}

function mediaBlock(m) {
  if (!m.media_url) return m.media_type ? el("div", "media", "[" + m.media_type + " — файл не сохранён]") : null;
  const box = el("div", "media");
  const mime = m.media_mime || "";
  if (mime.startsWith("image/")) { const i = el("img"); i.loading = "lazy"; i.src = m.media_url; box.append(i); }
  else if (mime.startsWith("video/")) { const v = el("video"); v.controls = true; v.preload = "none"; v.src = m.media_url; box.append(v); }
  else if (mime.startsWith("audio/")) { const a = el("audio"); a.controls = true; a.preload = "none"; a.src = m.media_url; box.append(a); }
  else { const a = el("a", "", "Скачать: " + (m.media_type || "файл")); a.href = m.media_url; box.append(a); }
  return box;
}

function card(m) {
  const c = el("div", "card");
  const meta = el("div", "meta");
  meta.append(el("b", "", m.sender_name || "—"), el("span", "", "в " + (m.chat_title || "—")), el("span", "", fmtTime(m.date)));
  if (m.deleted) meta.append(el("span", "badge del", "удалено"));
  if (m.edits.length) meta.append(el("span", "badge edit", "изменено ×" + m.edits.length));
  c.append(meta);
  if (m.text) {
    const t = el("div", "text clamp", m.text);
    t.title = "Нажмите, чтобы развернуть";
    t.onclick = () => t.classList.toggle("clamp");
    c.append(t);
  }
  const media = mediaBlock(m);
  if (media) c.append(media);
  if (m.edits.length) {
    const box = el("div", "edits");
    for (const e of m.edits) box.append(el("div", "old", e.old_text || "—"), el("div", "new", e.new_text || "—"));
    c.append(box);
  }
  return c;
}

async function loadMore() {
  if (state.loading || state.done) return;
  state.loading = true;
  const gen = state.gen;
  const data = await api("/api/messages", {...filters(), offset: state.offset});
  if (gen !== state.gen) { state.loading = false; return; }
  const list = $("list");
  let lastDay = list.dataset.lastDay || "";
  for (const m of data.items) {
    const k = dayKey(m.date);
    if (k !== lastDay) { list.append(el("div", "day", fmtDay(m.date))); lastDay = k; }
    list.append(card(m));
  }
  list.dataset.lastDay = lastDay;
  state.offset += data.items.length;
  if (data.total !== undefined) $("count").textContent = "Найдено: " + data.total;
  state.done = !data.has_more;
  $("sentinel").textContent = !state.offset ? "Ничего не найдено" : state.done ? "Это всё" : "";
  state.loading = false;
  if (!state.done) checkSentinel();
}

function resetList() {
  state.gen++; state.offset = 0; state.done = false; state.loading = false;
  $("list").replaceChildren(); $("list").dataset.lastDay = "";
  $("sentinel").textContent = "";
  window.scrollTo(0, 0);
  loadMore();
}

function checkSentinel() {
  if ($("sentinel").getBoundingClientRect().top < innerHeight + 600) loadMore();
}

function fill(sel, first, items, current) {
  sel.replaceChildren(new Option(first, ""));
  for (const [value, label] of items) sel.append(new Option(label, value));
  sel.value = items.some(i => String(i[0]) === String(current)) ? current : "";
}

async function loadFacets() {
  const f = await api("/api/facets", {db: state.db, kind: state.kind, ...range()});
  fill($("chat"), "Все чаты", f.chats.map(c => [c.chat_id, c.title + " (" + c.count + ")"]), state.chat);
  fill($("sender"), "Все отправители", f.senders.map(s => [s.name, s.name + " (" + s.count + ")"]), state.sender);
  const media = [["none", "Только текст (" + f.text_only + ")"], ...f.media.map(m => [m.type, m.type + " (" + m.count + ")"])];
  fill($("media"), "Любые вложения", media, state.media);
  state.chat = $("chat").value; state.sender = $("sender").value; state.media = $("media").value;
}

async function reloadAll() { await loadFacets(); resetList(); }

function setChip(value) {
  for (const b of $("chips").children) b.classList.toggle("on", b.dataset.d === value);
}

function applyChip(value) {
  setChip(value);
  if (value === "all") { state.from = state.to = ""; }
  else {
    const to = new Date(), from = new Date();
    from.setDate(from.getDate() - Number(value));
    if (value === "1") to.setDate(to.getDate() - 1);
    state.from = isoDay(from); state.to = isoDay(to);
  }
  $("from").value = state.from; $("to").value = state.to;
  reloadAll();
}

async function init() {
  const dbs = (await api("/api/dbs", {})).dbs;
  for (const d of dbs) $("db").append(new Option(d.label, d.id));
  state.db = dbs.length ? dbs[0].id : "";
  $("db").hidden = dbs.length < 2;
  $("db").onchange = e => { state.db = e.target.value; state.chat = state.sender = state.media = ""; reloadAll(); };
  for (const key of ["chat", "sender", "media"]) $(key).onchange = e => { state[key] = e.target.value; resetList(); };
  let t;
  $("q").oninput = e => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); resetList(); }, 250); };
  for (const k of ["deleted", "edited"]) {
    $("k-" + k).onclick = () => {
      state.kind = k; state.chat = state.sender = state.media = "";
      $("k-deleted").classList.toggle("on", k === "deleted");
      $("k-edited").classList.toggle("on", k === "edited");
      reloadAll();
    };
  }
  $("chips").onclick = e => { if (e.target.dataset.d) applyChip(e.target.dataset.d); };
  for (const id of ["from", "to"]) $(id).onchange = e => { state[id] = e.target.value; setChip(""); reloadAll(); };
  $("reset").onclick = () => {
    state.chat = state.sender = state.media = state.q = ""; $("q").value = "";
    applyChip("all");
  };
  $("refresh").onclick = reloadAll;
  $("top").onclick = () => window.scrollTo({top: 0, behavior: "smooth"});
  addEventListener("scroll", () => { $("top").hidden = scrollY < 800; checkSentinel(); }, {passive: true});
  const header = document.querySelector("header");
  new ResizeObserver(() => document.documentElement.style.setProperty("--hh", header.offsetHeight + "px")).observe(header);
  setInterval(() => { if (state.offset <= PAGE_SIZE && !state.q && scrollY < 200 && document.visibilityState === "visible") reloadAll(); }, 30000);
  reloadAll();
}
const PAGE_SIZE = 50;
init();
</script>
</body>
</html>
"""


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _int_param(query: dict, key: str) -> int | None:
    value = query.get(key, [""])[0]
    return int(value) if value.lstrip("-").isdigit() else None


def _scope(query: dict, *, with_filters: bool) -> tuple[str, list]:
    """WHERE clause for the chosen kind and date range; with_filters adds chat/sender/media/search.

    Facets (the dropdown contents) use the kind and date range only, so the
    dropdowns stay stable while the user narrows the list with them.
    """
    if query.get("kind", ["deleted"])[0] == "edited":
        where = ["EXISTS (SELECT 1 FROM edits e WHERE e.chat_id = m.chat_id AND e.msg_id = m.msg_id)"]
    else:
        where = ["m.deleted = 1"]
    params: list = []

    date_from, date_to = _int_param(query, "from"), _int_param(query, "to")
    if date_from is not None:
        where.append("m.date >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("m.date < ?")
        params.append(date_to)
    if not with_filters:
        return " AND ".join(where), params

    chat = _int_param(query, "chat")
    if chat is not None:
        where.append("m.chat_id = ?")
        params.append(chat)
    sender = query.get("sender", [""])[0]
    if sender:
        where.append("m.sender_name = ?")
        params.append(sender)
    media = query.get("media", [""])[0]
    if media == "none":
        where.append("m.media_type IS NULL")
    elif media:
        where.append("m.media_type = ?")
        params.append(media)
    text = query.get("q", [""])[0]
    if text:
        where.append("(m.text LIKE ? ESCAPE '\\' OR m.sender_name LIKE ? ESCAPE '\\' OR m.chat_title LIKE ? ESCAPE '\\')")
        params += [_like(text)] * 3
    return " AND ".join(where), params


class ViewerHandler(BaseHTTPRequestHandler):
    databases: dict[str, Path] = {}
    labels: dict[str, str] = {}
    token: str = ""

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _connect(self, name: str) -> sqlite3.Connection | None:
        path = self.databases.get(name)
        if path is None:
            return None
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authorized(self, query: dict) -> tuple[bool, dict | None]:
        if not self.token:
            return True, None
        cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        supplied = cookie["viewer_token"].value if "viewer_token" in cookie else ""
        if hmac.compare_digest(supplied, self.token):
            return True, None
        supplied = query.get("token", [""])[0]
        if hmac.compare_digest(supplied, self.token):
            return True, {"Set-Cookie": f"viewer_token={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"}
        return False, None

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        ok, cookie_header = self._authorized(query)
        if not ok:
            self._send(401, "Нужен токен: откройте /?token=...".encode(), "text/plain; charset=utf-8")
            return
        if url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8", cookie_header)
        elif url.path == "/api/dbs":
            self._json({"dbs": [{"id": name, "label": self.labels.get(name, name)} for name in self.databases]})
        elif url.path == "/api/facets":
            self._facets(query)
        elif url.path == "/api/messages":
            self._messages(query)
        elif url.path == "/media":
            self._media(query)
        else:
            self._send(404, b"not found", "text/plain")

    def _facets(self, query: dict) -> None:
        conn = self._connect(query.get("db", [""])[0])
        if conn is None:
            self._json({"chats": [], "senders": [], "media": [], "text_only": 0})
            return
        where, params = _scope(query, with_filters=False)
        base = f"FROM messages m WHERE {where}"
        with conn:
            chats = conn.execute(
                f"SELECT m.chat_id, COALESCE(MAX(m.chat_title), CAST(m.chat_id AS TEXT)) AS title, COUNT(*) AS count "
                f"{base} GROUP BY m.chat_id ORDER BY count DESC", params).fetchall()
            senders = conn.execute(
                f"SELECT m.sender_name AS name, COUNT(*) AS count {base} AND m.sender_name IS NOT NULL "
                f"GROUP BY m.sender_name ORDER BY count DESC LIMIT 300", params).fetchall()
            media = conn.execute(
                f"SELECT m.media_type AS type, COUNT(*) AS count {base} AND m.media_type IS NOT NULL "
                f"GROUP BY m.media_type ORDER BY count DESC", params).fetchall()
            text_only = conn.execute(f"SELECT COUNT(*) {base} AND m.media_type IS NULL", params).fetchone()[0]
        conn.close()
        self._json({
            "chats": [dict(r) for r in chats], "senders": [dict(r) for r in senders],
            "media": [dict(r) for r in media], "text_only": text_only,
        })

    def _messages(self, query: dict) -> None:
        db_name = query.get("db", [""])[0]
        conn = self._connect(db_name)
        if conn is None:
            self._json({"items": [], "has_more": False, "total": 0})
            return
        where, params = _scope(query, with_filters=True)
        offset = max(_int_param(query, "offset") or 0, 0)
        with conn:
            total = conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {where}", params).fetchone()[0] if offset == 0 else None
            rows = conn.execute(
                f"SELECT m.* FROM messages m WHERE {where} ORDER BY m.date DESC, m.msg_id DESC LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE + 1, offset),
            ).fetchall()
            items = [self._item(conn, db_name, row) for row in rows[:PAGE_SIZE]]
        conn.close()
        payload = {"items": items, "has_more": len(rows) > PAGE_SIZE}
        if total is not None:
            payload["total"] = total
        self._json(payload)

    def _item(self, conn: sqlite3.Connection, db_name: str, row: sqlite3.Row) -> dict:
        edits = conn.execute(
            "SELECT old_text, new_text, date FROM edits WHERE chat_id = ? AND msg_id = ? ORDER BY date",
            (row["chat_id"], row["msg_id"]),
        ).fetchall()
        media_url, media_mime = None, None
        if row["media_path"] and self._media_file(row["media_path"]):
            media_url = f"/media?db={db_name}&chat={row['chat_id']}&msg={row['msg_id']}"
            media_mime = mimetypes.guess_type(row["media_path"])[0]
        return {
            "chat_id": row["chat_id"], "msg_id": row["msg_id"], "chat_title": row["chat_title"],
            "sender_name": row["sender_name"], "text": row["text"], "media_type": row["media_type"],
            "media_url": media_url, "media_mime": media_mime, "date": row["date"],
            "deleted": bool(row["deleted"]), "edits": [dict(e) for e in edits],
        }

    @staticmethod
    def _media_file(relative: str) -> Path | None:
        """Resolve a stored media path, refusing anything outside the project folder."""
        candidate = (BASE_DIR / relative).resolve()
        if BASE_DIR in candidate.parents and candidate.is_file():
            return candidate
        return None

    def _media(self, query: dict) -> None:
        conn = self._connect(query.get("db", [""])[0])
        chat, msg = _int_param(query, "chat"), _int_param(query, "msg")
        if conn is None or chat is None or msg is None:
            self._send(404, b"not found", "text/plain")
            return
        with conn:
            row = conn.execute("SELECT media_path FROM messages WHERE chat_id = ? AND msg_id = ?", (chat, msg)).fetchone()
        conn.close()
        path = self._media_file(row["media_path"]) if row and row["media_path"] else None
        if path is None:
            self._send(404, b"not found", "text/plain")
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), mime)


def main() -> None:
    parser = argparse.ArgumentParser(description="Веб-просмотр удалённых и изменённых сообщений.")
    parser.add_argument(
        "--db", action="append", metavar="ФАЙЛ[=НАЗВАНИЕ]",
        help="Файл БД и, через =, название в списке (можно несколько раз). По умолчанию все *.db в папке.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Адрес (по умолчанию только localhost)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    specs = [spec.partition("=") for spec in args.db] if args.db else [(str(p), "", "") for p in sorted(BASE_DIR.glob("*.db"))]
    paths = [(Path(path), label) for path, _, label in specs]
    ViewerHandler.databases = {p.name: p.resolve() for p, _ in paths if p.exists()}
    ViewerHandler.labels = {p.name: label for p, label in paths if label}
    if not ViewerHandler.databases:
        raise SystemExit("Не найдено ни одной базы данных (*.db)")
    ViewerHandler.token = os.environ.get("VIEWER_TOKEN", "")
    if args.host not in ("127.0.0.1", "localhost") and not ViewerHandler.token:
        raise SystemExit("Для доступа не с localhost задайте VIEWER_TOKEN")

    print(f"Базы: {', '.join(ViewerHandler.databases)}\nhttp://{args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), ViewerHandler).serve_forever()


if __name__ == "__main__":
    main()
