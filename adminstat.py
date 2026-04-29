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


def init_admin_settings():
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_settings (
            username TEXT PRIMARY KEY,
            media_group_mode BOOLEAN DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()


def set_media_group_mode(admin, mode: bool):
    init_admin_settings()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO admin_settings (username, media_group_mode) VALUES (?, ?)', (admin, int(mode)))
    conn.commit()
    conn.close()


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


def init_statistics_db():
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS statistics (
            username TEXT PRIMARY KEY,
            postcount INTEGER DEFAULT 0,
            queuedcount INTEGER DEFAULT 0,
            viewstotal INTEGER DEFAULT 0,
            reactionstotal INTEGER DEFAULT 0
        )
    ''')
    cursor.execute(f'ATTACH DATABASE "{MESSAGES_DB}" AS messages')
    admins = get_admin_uns()
    for admin in admins:
        cursor.execute('SELECT COUNT(*) FROM messages WHERE username = ? AND posted = 1', (admin,))
        postcount = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM messages WHERE username = ? AND posted = 0', (admin,))
        queuedcount = cursor.fetchone()[0]
        cursor.execute('INSERT OR REPLACE INTO statistics (username, postcount, queuedcount, viewstotal, reactionstotal) VALUES (?, ?, ?, 0, 0)', (admin, postcount, queuedcount))
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
    init_statistics_db()

    if days is None:
        update_views_reactions_count()
        conn = sqlite3.connect(STATISTICS_DB)
        cursor = conn.cursor()
        cursor.execute('SELECT username, postcount, queuedcount, viewstotal, reactionstotal FROM statistics')
        rows = cursor.fetchall()
        conn.close()
        return [{'username': row[0], 'postcount': row[1], 'queuedcount': row[2], 'viewstotal': row[3], 'reactionstotal': row[4], 'engagement': round((row[4] / row[3] * 100), 2) if row[3] > 0 else 0} for row in rows]

    if days <= 0:
        days = 1

    now = timezone.tz_now()
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    period_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    stat_conn = sqlite3.connect(STATISTICS_DB)
    msg_conn = sqlite3.connect(MESSAGES_DB)
    stat_conn.row_factory = sqlite3.Row

    cursor = stat_conn.cursor()
    cursor.execute('SELECT username FROM statistics')
    all_admins = [row[0] for row in cursor.fetchall()]
    stat_conn.close()

    cursor = msg_conn.cursor()
    cursor.execute(
        '''
        SELECT
            m.username,
            COUNT(*) as postcount,
            COALESCE(SUM(m.views), 0) as viewstotal,
            COALESCE(SUM(m.reactions), 0) as reactionstotal
        FROM messages m
        WHERE m.posted = TRUE
        AND m.posted_at IS NOT NULL
        AND m.posted_at >= ?
        AND m.posted_at <= ?
        GROUP BY m.username
        ''',
        (period_start.isoformat(), period_end.isoformat())
    )
    period_rows = {row[0]: row for row in cursor.fetchall()}

    result = []
    for username in all_admins:
        if username in period_rows:
            row = period_rows[username]
            postcount, viewstotal, reactionstotal = row[1], row[2], row[3]
        else:
            postcount, viewstotal, reactionstotal = 0, 0, 0

        queued_cursor = msg_conn.cursor()
        queued_cursor.execute(
            'SELECT COUNT(*) FROM messages WHERE username = ? AND posted = FALSE',
            (username,)
        )
        queuedcount = queued_cursor.fetchone()[0]

        rate = round(reactionstotal / viewstotal * 100, 2) if viewstotal > 0 else 0.0
        result.append({
            'username': username,
            'postcount': postcount,
            'queuedcount': queuedcount,
            'viewstotal': viewstotal,
            'reactionstotal': reactionstotal,
            'engagement': rate,
        })

    msg_conn.close()
    return result  

def load_top_posts(days: int | None = None, limit: int = 10):
    load_dotenv(override=True)
    channel_id = int(os.getenv('CHANNEL_ID', 0))
    # Для приватных каналов ID вида -100XXXXXXXXX → убираем -100
    channel_id_url = str(channel_id).replace('-100', '') if channel_id < 0 else str(channel_id)

    now = timezone.tz_now()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()

    if days is not None:
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        period_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        cursor.execute(
            '''
            SELECT message_id, username, posted_at, views, reactions,
                   CASE WHEN views > 0 THEN ROUND(CAST(reactions AS FLOAT) / views * 100, 2) ELSE 0.0 END as engagement,
                   current_message_id
            FROM messages
            WHERE posted = TRUE AND posted_at IS NOT NULL
            AND posted_at >= ? AND posted_at <= ?
            ORDER BY views DESC
            LIMIT ?
            ''',
            (period_start.isoformat(), period_end.isoformat(), limit)
        )
    else:
        cursor.execute(
            '''
            SELECT message_id, username, posted_at, views, reactions,
                   CASE WHEN views > 0 THEN ROUND(CAST(reactions AS FLOAT) / views * 100, 2) ELSE 0.0 END as engagement,
                   current_message_id
            FROM messages
            WHERE posted = TRUE AND posted_at IS NOT NULL
            ORDER BY views DESC
            LIMIT ?
            ''',
            (limit,)
        )

    rows = cursor.fetchall()
    conn.close()
    return [
        {
            'message_id': row[0],
            'username': row[1],
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
                post["quality"],
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

    title = "Топ админов по качеству"
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


def save_stat(stat):
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    for item in stat:
        cursor.execute('INSERT OR REPLACE INTO statistics (username, postcount, queuedcount) VALUES (?, ?, ?)',
                       (item['username'], item['postcount'], item.get('queuedcount', 0)))
    conn.commit()
    conn.close()


def add_post_to_count(admin):
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('UPDATE statistics SET postcount = postcount + 1 WHERE username = ?', (admin,))
    conn.commit()
    conn.close()


def add_queued_to_count(admin):
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('UPDATE statistics SET queuedcount = queuedcount + 1 WHERE username = ?', (admin,))
    conn.commit()
    conn.close()


def decrement_queued_to_count(admin):
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('UPDATE statistics SET queuedcount = queuedcount - 1 WHERE username = ? AND queuedcount > 0', (admin,))
    conn.commit()
    conn.close()

def update_views_reactions_count():
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute(f'ATTACH DATABASE "{MESSAGES_DB}" AS messages')
    cursor.execute('''
        UPDATE statistics
        SET
        viewstotal = COALESCE((
            SELECT SUM(m.views)
            FROM messages.messages m
            WHERE m.username = statistics.username
        ), 0),
        reactionstotal = COALESCE((
            SELECT SUM(m.reactions)
            FROM messages.messages m
            WHERE m.username = statistics.username
        ), 0)
    ''')
    conn.commit()
    conn.close()


def reset_statistics():
    init_statistics_db()
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()
    cursor.execute('UPDATE statistics SET postcount = 0, queuedcount = 0')
    conn.commit()
    conn.close()
