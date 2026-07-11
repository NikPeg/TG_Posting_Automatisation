import sqlite3
import os
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
import timezone
import csv
import io

STATISTICS_DB = "statistics.db"
MESSAGES_DB = "messages.db"

logger = logging.getLogger(__name__)


def get_admin_uns():
    load_dotenv(override=True)
    ADMIN_UNS = [admin_us for admin_us in os.getenv('ADMIN_UNS', '').split(',')]
    return ADMIN_UNS


def get_admin_ids():
    load_dotenv(override=True)
    ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',')]
    return ADMIN_IDS


def get_admin_id_by_un(username):
    return dict(zip(get_admin_uns(), get_admin_ids())).get(username)


def get_admin_un_by_id(user_id):
    return dict(zip(get_admin_ids(), get_admin_uns())).get(user_id)


def init_admin_settings():
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_settings (
            username TEXT PRIMARY KEY,
            media_group_mode BOOLEAN DEFAULT 1,
            weekday TEXT DEFAULT NULL
        )
    ''')
    try:
        cursor.execute('ALTER TABLE admin_settings ADD COLUMN weekday TEXT DEFAULT NULL')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def set_media_group_mode(admin, mode: bool):
    init_admin_settings()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO admin_settings (username, media_group_mode) VALUES (?, ?)', (admin, int(mode)))
    conn.commit()
    conn.close()


def set_weekday_mode(admin, weekday: str | None):
    init_admin_settings()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO admin_settings (username, weekday) VALUES (?, ?) '
        'ON CONFLICT(username) DO UPDATE SET weekday = excluded.weekday',
        (admin, weekday)
    )
    conn.commit()
    conn.close()


def get_weekday_mode(admin) -> str | None:
    init_admin_settings()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('SELECT weekday FROM admin_settings WHERE username = ?', (admin,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_media_group_mode(admin) -> bool:
    init_admin_settings()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('SELECT media_group_mode FROM admin_settings WHERE username = ?', (admin,))
    row = cursor.fetchone()
    conn.close()
    if row is None: #вкл по умолч
        return True
    return bool(row[0])


def drop_legacy_statistics_table():
    """Миграция: таблица statistics больше не используется — статистика
    считается напрямую из messages по списку админов из .env"""
    conn = sqlite3.connect(STATISTICS_DB)
    conn.execute('DROP TABLE IF EXISTS statistics')
    conn.commit()
    conn.close()


def export_admin_stat_csv(stat):
    filename = f"admin_stat_{datetime.now().date()}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter = ';')
        writer.writerow([
            "username",
            "posts",
            "queued",
            "views",
            "reactions",
            "отклик"
        ])
        for adm in stat:
            views = adm["viewstotal"]
            reactions = adm["reactionstotal"]
            rate = round(reactions / views * 100, 2) if views > 0 else 0.0
            writer.writerow([
                adm["username"],
                adm["postcount"],
                adm["queuedcount"],
                views,
                reactions,
                rate
            ])
    return filename


def load_stat(days: int | None = None):
    where = 'posted = TRUE'
    params: list = []

    if days is not None:
        if days <= 0:
            days = 1
        now = timezone.tz_now()
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        period_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        where += ' AND posted_at IS NOT NULL AND posted_at >= ? AND posted_at <= ?'
        params = [period_start.isoformat(), period_end.isoformat()]

    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute(
        f'''
        SELECT
            user_id,
            COUNT(*) as postcount,
            COALESCE(SUM(views), 0) as viewstotal,
            COALESCE(SUM(reactions), 0) as reactionstotal
        FROM messages
        WHERE {where}
        GROUP BY user_id
        ''',
        params
    )
    posted_rows = {row[0]: row for row in cursor.fetchall()}

    cursor.execute('SELECT user_id, COUNT(*) FROM messages WHERE posted = FALSE GROUP BY user_id')
    queued_rows = dict(cursor.fetchall())
    conn.close()

    result = []
    for username, user_id in zip(get_admin_uns(), get_admin_ids()):
        row = posted_rows.get(user_id)
        postcount, viewstotal, reactionstotal = (row[1], row[2], row[3]) if row else (0, 0, 0)
        queuedcount = queued_rows.get(user_id, 0)
        rate = round(reactionstotal / viewstotal * 100, 2) if viewstotal > 0 else 0.0
        result.append({
            'username': username,
            'postcount': postcount,
            'queuedcount': queuedcount,
            'viewstotal': viewstotal,
            'reactionstotal': reactionstotal,
            'engagement': rate,
        })
    return result

def load_top_posts(days: int | None = None, limit: int = 10, username: str | None = None):
    load_dotenv(override=True)
    channel_id = int(os.getenv('CHANNEL_ID', 0))
    # Для приватных каналов ID вида -100XXXXXXXXX → убираем -100
    channel_id_url = str(channel_id).replace('-100', '') if channel_id < 0 else str(channel_id)

    now = timezone.tz_now()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()

    # Сортируем по отклику (reactions / views).
    # Посты без просмотров идут в конец (engagement = 0), но не отсекаются —
    # чтобы не терять посты с реакциями при нулевых просмотрах в Telethon-кэше.
    engagement_expr = 'CASE WHEN views > 0 THEN CAST(reactions AS FLOAT) / views ELSE 0.0 END'

    where_parts = ['posted = TRUE', 'posted_at IS NOT NULL']
    params: list = []

    if username:
        admin_id = get_admin_id_by_un(username)
        if admin_id is not None:
            where_parts.append('user_id = ?')
            params.append(admin_id)
        else:
            where_parts.append('username = ?')
            params.append(username)

    if days is not None:
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        period_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        where_parts.append('posted_at >= ?')
        where_parts.append('posted_at <= ?')
        params += [period_start.isoformat(), period_end.isoformat()]

    where_clause = ' AND '.join(where_parts)
    params.append(limit)

    cursor.execute(
        f'''
        SELECT message_id, username, posted_at, views, reactions,
               ROUND({engagement_expr} * 100, 2) as engagement,
               current_message_id, user_id
        FROM messages
        WHERE {where_clause}
        ORDER BY {engagement_expr} DESC, reactions DESC, views DESC
        LIMIT ?
        ''',
        params
    )

    rows = cursor.fetchall()
    conn.close()
    un_by_id = dict(zip(get_admin_ids(), get_admin_uns()))
    return [
        {
            'message_id': row[0],
            'username': row[1] or un_by_id.get(row[7]),
            'posted_at': row[2],
            'views': row[3],
            'reactions': row[4],
            'engagement': row[5],
            'url': f"https://t.me/c/{channel_id_url}/{row[6]}" if row[6] else "",
        }
        for row in rows
    ]


def export_top_posts_csv(posts):
    filename = f"top_posts_{datetime.now().date()}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(["username", "posted_at", "views", "reactions", "отклик", "url"])
        for post in posts:
            writer.writerow([
                post["username"],
                post["posted_at"],
                post["views"],
                post["reactions"],
                post["engagement"],
                post["url"],
            ])
    return filename


def render_best_admins_image(stat: list, days: int | None = None) -> io.BytesIO:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    top = sorted(
        stat,
        key=lambda x: (x['engagement'], x['postcount'], x['reactionstotal'], x['viewstotal'], x['queuedcount']),
        reverse=True
    )[:5]
    if not top:
        raise ValueError("Нет данных для отображения")

    fig, ax = plt.subplots(figsize=(9, 2.2 + 0.55 * len(top)))
    ax.axis('off')

    title = "Топ админов по отклику"
    if days:
        title += f" — последние {days} дн."
    fig.suptitle(title, fontsize=14, fontweight='bold', y=0.98)

    cols = ["#", "Админ", "Посты", "В очереди", "Просмотры", "Реакции", "Отклик"]
    rows = [
        [
            str(i + 1),
            f"@{a['username']}",
            str(a['postcount']),
            str(a['queuedcount']),
            str(a['viewstotal']),
            str(a['reactionstotal']),
            f"{a['engagement']:.2f}",
        ]
        for i, a in enumerate(top)
    ]

    col_widths = [0.05, 0.22, 0.10, 0.13, 0.15, 0.15, 0.13]
    table = ax.table(
        cellText=rows,
        colLabels=cols,
        cellLoc='center',
        loc='center',
        colWidths=col_widths,
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.6)

    for (row_idx, col_idx), cell in table.get_celld().items():
        cell.set_edgecolor('#cccccc')
        if row_idx == 0:
            cell.set_facecolor('#2f3d4e')
            cell.set_text_props(color='white', fontweight='bold')
        elif row_idx % 2 == 0:
            cell.set_facecolor('#f2f4f6')
        else:
            cell.set_facecolor('#ffffff')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf


