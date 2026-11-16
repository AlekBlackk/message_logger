"""Export deleted messages from SQLite database to a user-friendly HTML file."""

import datetime
import html
import sqlite3
from pathlib import Path


def export_to_html(db_path: str = "messages.db", output_path: str = "deleted_messages.html"):
    if not Path(db_path).exists():
        print(f"База {db_path} не найдена!")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT date, chat_title, sender_name, text, media_type, media_path
        FROM messages
        WHERE deleted = 1
        ORDER BY date DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    table_rows = []
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M:%S")
        chat = html.escape(r[1] or "—")
        sender = html.escape(r[2] or "—")
        text = html.escape(r[3] or "")
        media = html.escape(r[4] or "")
        media_path = html.escape(r[5] or "")

        media_info = f'<span class="badge">{media}</span>' if media else "—"
        if media_path:
            media_info += f'<br><span class="media-path">{media_path}</span>'

        text_display = text if text else '<span style="color:#64748b; font-style:italic;">(без текста)</span>'

        table_rows.append(f"""
            <tr>
                <td class="date">{dt}</td>
                <td class="chat">{chat}</td>
                <td class="sender">{sender}</td>
                <td class="text">{text_display}</td>
                <td>{media_info}</td>
            </tr>""")

    rows_html = "".join(table_rows)
    total_count = len(rows)

    html_template = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Удалённые сообщения</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            margin: 0;
            padding: 30px 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        h1 {
            color: #38bdf8;
            margin: 0 0 6px 0;
            font-size: 26px;
        }
        .subtitle {
            color: #94a3b8;
            margin-bottom: 20px;
            font-size: 15px;
        }
        #search {
            width: 100%;
            max-width: 450px;
            padding: 12px 16px;
            margin-bottom: 20px;
            border-radius: 8px;
            border: 1px solid #334155;
            background: #1e293b;
            color: #f8fafc;
            font-size: 15px;
            outline: none;
        }
        #search:focus {
            border-color: #38bdf8;
        }
        .table-wrap {
            overflow-x: auto;
            background: #1e293b;
            border-radius: 10px;
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.4);
            border: 1px solid #334155;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 14px;
        }
        th {
            background: #1e293b;
            color: #94a3b8;
            padding: 14px 16px;
            font-weight: 600;
            border-bottom: 2px solid #334155;
            position: sticky;
            top: 0;
        }
        td {
            padding: 12px 16px;
            border-bottom: 1px solid #334155;
            vertical-align: top;
        }
        tr:hover td {
            background: #273549;
        }
        .date {
            color: #38bdf8;
            font-weight: 500;
            white-space: nowrap;
        }
        .chat {
            color: #c084fc;
            font-weight: 600;
            white-space: nowrap;
        }
        .sender {
            color: #4ade80;
            font-weight: 500;
            white-space: nowrap;
        }
        .text {
            color: #f8fafc;
            line-height: 1.5;
            word-break: break-word;
        }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 12px;
            background: #0369a1;
            color: #e0f2fe;
            font-weight: 500;
        }
        .media-path {
            display: inline-block;
            margin-top: 4px;
            color: #94a3b8;
            font-size: 12px;
            word-break: break-all;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Удалённые сообщения</h1>
        <div class="subtitle">Всего удалённых сообщений в базе: <strong>__COUNT__</strong></div>
        <input type="text" id="search" placeholder="Быстрый поиск по тексту, имени или чату..." onkeyup="filterTable()">
        
        <div class="table-wrap">
            <table id="msgTable">
                <thead>
                    <tr>
                        <th>Дата и время</th>
                        <th>Чат</th>
                        <th>Отправитель</th>
                        <th>Текст сообщения</th>
                        <th>Медиа</th>
                    </tr>
                </thead>
                <tbody>
                    __ROWS__
                </tbody>
            </table>
        </div>
    </div>

    <script>
        function filterTable() {
            var input = document.getElementById("search");
            var filter = input.value.toLowerCase();
            var table = document.getElementById("msgTable");
            var tr = table.getElementsByTagName("tr");

            for (var i = 1; i < tr.length; i++) {
                var rowText = tr[i].textContent || tr[i].innerText;
                tr[i].style.display = rowText.toLowerCase().indexOf(filter) > -1 ? "" : "none";
            }
        }
    </script>
</body>
</html>"""

    content = html_template.replace("__COUNT__", str(total_count)).replace("__ROWS__", rows_html)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Готово! Создан файл: {output_path} ({total_count} удаленных сообщений)")


if __name__ == "__main__":
    export_to_html()
