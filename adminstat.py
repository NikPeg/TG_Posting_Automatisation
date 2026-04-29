import sqlite3
import os
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
import timezone
import csv

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
            "reactions"
        ])
        for adm in stat:
            writer.writerow([
                adm["username"],
                adm["postcount"],
                adm["queuedcount"],
                adm["viewstotal"],
                adm["reactionstotal"]
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
        return [{'username': row[0], 'postcount': row[1], 'queuedcount': row[2], 'viewstotal': row[3], 'reactionstotal': row[4], 'reaction_rate': round((row[4] / row[3] * 100), 2) if row[3] > 0 else 0} for row in rows]

    if days <= 0:
        days = 1

    now = timezone.tz_now()
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    period_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT
            m.username,
            COUNT(*) as postcount,
            (
                SELECT COUNT(*)
                FROM messages q
                WHERE q.username = m.username
                AND q.posted = FALSE
            ) as queuedcount,
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
    rows = cursor.fetchall()
    conn.close()
    return [{'username': row[0], 'postcount': row[1], 'queuedcount': row[2], 'viewstotal': row[3], 'reactionstotal': row[4], 'reaction_rate': round((row[4] / row[3] * 100), 2) if row[3] > 0 else 0} for row in rows]  

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
