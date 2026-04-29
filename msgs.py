import sqlite3
import logging
from aiogram import types
import os
from telethon import TelegramClient
from dotenv import load_dotenv
import csv
from datetime import datetime
import timezone
import adminstat

MESSAGES_DB = "messages.db"

logger = logging.getLogger(__name__)


def init_messages_db():
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            user_id INTEGER,
            current_message_id INTEGER,
            posted BOOLEAN DEFAULT FALSE,
            is_forwarded_from_channel BOOLEAN DEFAULT FALSE,
            views INTEGER DEFAULT 0,
            reactions INTEGER DEFAULT 0,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN media_group INTEGER DEFAULT NULL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN posted_at TEXT DEFAULT NULL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN user_id INTEGER DEFAULT NULL')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def get_media_group_ids(mg_id):
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('SELECT message_id FROM messages WHERE media_group = ?', 
                   (mg_id,))
    rows = cursor.fetchall()
    print(rows)
    conn.close()
    return [row[0] for row in rows]


def _format_posted_at(raw: str | None) -> str:
    if not raw:
        return ''
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime('%Y/%m/%d %H:%M:%S')
    except Exception:
        return raw


def export_msgs_csv(stat):
    filename = f"msgs_{datetime.now().date()}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter = ';')
        writer.writerow([
            "message_id",
            "current_message_id",
            "username",
            "views",
            "reactions",
            "posted",
            "posted_at"
        ])
        for msg in stat:
            writer.writerow([
                msg["message_id"],
                msg["current_message_id"],
                msg["username"],
                msg["views"],
                msg["reactions"],
                msg["posted"],
                _format_posted_at(msg.get("posted_at"))
            ])
    return filename


def load_messages():
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('SELECT message_id, chat_id, username, user_id, current_message_id, posted, is_forwarded_from_channel, views, reactions, media_group, posted_at FROM messages WHERE posted = FALSE')
    rows = cursor.fetchall()
    conn.close()
    return [{'message_id': row[0], 'chat_id': row[1], 'username': row[2], 'user_id': row[3], 'current_message_id': row[4], 'posted': bool(row[5]), 'is_forwarded_from_channel': bool(row[6]), 'views': row[7], 'reactions': row[8], 'media_group': row[9], 'posted_at': row[10]} for row in rows]


def load_all_messages():
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('SELECT message_id, chat_id, username, user_id, current_message_id, posted, is_forwarded_from_channel, views, reactions, media_group, posted_at FROM messages')
    rows = cursor.fetchall()
    conn.close()
    return [{'message_id': row[0], 'chat_id': row[1], 'username': row[2], 'user_id': row[3], 'current_message_id': row[4], 'posted': bool(row[5]), 'is_forwarded_from_channel': bool(row[6]), 'views': row[7], 'reactions': row[8], 'media_group': row[9], 'posted_at': row[10]} for row in rows]


def save_messages(messages):
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    for msg in messages:
        cursor.execute('INSERT OR REPLACE INTO messages (message_id, chat_id, username, user_id, current_message_id, posted, is_forwarded_from_channel, views, reactions, media_group) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (msg['message_id'], msg['chat_id'], msg['username'], msg.get('user_id'), msg.get('current_message_id'), msg.get('posted', False), msg.get('is_forwarded_from_channel', False), msg.get('views', 0), msg.get('reactions', 0), msg.get('media_group', None)))
    conn.commit()
    conn.close()


def clear_messages():
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM messages')
    conn.commit()
    conn.close()


def clear_message(msg_id, adm_id):
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM messages WHERE message_id = ? AND user_id = ?', 
                   (msg_id, adm_id))
    num = cursor.rowcount
    conn.commit()
    conn.close()
    return num


def clear_posted_messages():
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM messages WHERE posted = TRUE')
    conn.commit()
    conn.close()
    logger.info("Опубликованные сообщения удалены из базы данных")


def save_message_to_db(message: types.Message):
    init_messages_db()

    media_group_mode = adminstat.get_media_group_mode(message.from_user.id)
    if hasattr(message, 'media_group_id') and media_group_mode:
        media_group_id = message.media_group_id
        mgid = f' - медиа группа {media_group_id}'
    else: media_group_id = None; mgid = ''

    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    is_forwarded_from_channel = message.forward_origin is not None and hasattr(message.forward_origin, 'chat') and message.forward_origin.chat.type in ['channel']
    cursor.execute('INSERT OR IGNORE INTO messages (message_id, chat_id, username, user_id, posted, is_forwarded_from_channel, views, reactions, media_group) VALUES (?, ?, ?, ?, FALSE, ?, ?, ?, ?)',
                   (message.message_id, message.chat.id, message.from_user.username, message.from_user.id, is_forwarded_from_channel, 0, 0, media_group_id))
    conn.commit()
    conn.close()

    if message.from_user and message.from_user.username:
        adminstat.add_queued_to_count(message.from_user.id)
        logger.info(f"Сообщение {message.message_id}{mgid} сохранено в базу данных от пользователя {message.from_user.username}")

    return {
        'message_id': message.message_id,
        'chat_id': message.chat.id,
        'username': message.from_user.username,
        'user_id': message.from_user.id,
        'posted': False,
        'is_forwarded_from_channel': is_forwarded_from_channel,
        'views': 0, 
        'reactions': 0,
        'media_group': media_group_id
    }


def update_message_posted(message_id, chat_id, current_message_id):
    init_messages_db()
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()
    cursor.execute(
    'UPDATE messages SET current_message_id = ?, posted = TRUE, posted_at = ? WHERE message_id = ? AND chat_id = ?',
        (current_message_id, timezone.tz_now().isoformat(), message_id, chat_id))
    conn.commit()
    conn.close()
    logger.info(f'Сообщение {message_id} обновлено как опубликованное с новым ID {current_message_id}')


async def collect_message_stats():
    load_dotenv(override=True)
    api_id = os.getenv('CORE_API_ID')
    api_hash = os.getenv('CORE_API_HASH')
    channel_id = int(os.getenv('CHANNEL_ID'))

    if not api_id or not api_hash:
        logger.error("CORE_API_ID or CORE_API_HASH not found in .env")
        return
    try:
        async with TelegramClient('session', int(api_id), api_hash) as client:
            init_messages_db()
            conn = sqlite3.connect(MESSAGES_DB)
            cursor = conn.cursor()
            cursor.execute('SELECT message_id, chat_id, current_message_id FROM messages WHERE posted = TRUE AND current_message_id IS NOT NULL')
            published_messages = cursor.fetchall()
            conn.close()

            for msg_id, chat_id, current_msg_id in published_messages:
                try:
                    message = await client.get_messages(channel_id, ids=current_msg_id)
                    if message:
                        views = getattr(message, 'views', 0) or 0
                        reactions_count = 0
                        if hasattr(message, 'reactions') and message.reactions:
                            reactions_count = sum(r.count for r in message.reactions.results) if message.reactions.results else 0

                        conn = sqlite3.connect(MESSAGES_DB)
                        cursor = conn.cursor()
                        now = timezone.tz_now()
                        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                        day_end = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
                        cursor.execute('UPDATE messages SET views = ?, reactions = ? '
                                       'WHERE message_id = ? AND chat_id = ?'
                                       'AND posted_at >= ? AND posted_at <= ?',
                                    (views, reactions_count, msg_id, chat_id, day_start, day_end))
                        conn.commit()
                        conn.close()
                        logger.info(f"Updated stats for message {current_msg_id}: views={views}, reactions={reactions_count}")
                    else:
                        logger.warning(f"Message {current_msg_id} not found in channel")
                except Exception as e:
                    logger.error(f"Error fetching stats for message {current_msg_id}: {e}")
    except Exception as e:
        logger.error(f"Error in collect_message_stats: {e}")


async def update_user_ids():
    usernames = os.getenv("ADMIN_UNS", "").split(",")
    user_ids = os.getenv("ADMIN_IDS", "").split(",")

    if len(usernames) != len(user_ids):
        logger.error("Ошибка: Списки имен и ID разной длины!")
        return

    data_to_update = list(zip(user_ids, usernames))

    try:
        conn = sqlite3.connect(MESSAGES_DB)
        cursor = conn.cursor()
        
        cursor.executemany("UPDATE messages SET user_id = ? WHERE username = ?", data_to_update)
        
        conn.commit()
        logger.info(f"Успешно обновлено типов пользователей: {len(data_to_update)}")
        
    except sqlite3.Error as e:
        logger.error(f"Ошибка при работе с базой: {e}")
    finally:
        if conn:
            conn.close()
