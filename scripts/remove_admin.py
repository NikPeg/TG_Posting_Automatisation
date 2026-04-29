#!/usr/bin/env python3
"""
Полное удаление админа из всех баз данных.
Использование: python scripts/remove_admin.py <username>
Пример:        python scripts/remove_admin.py gesegnet_she
"""

import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATISTICS_DB = "statistics.db"
MESSAGES_DB = "messages.db"


def remove_admin(username: str):
    username = username.lstrip('@')
    print(f"Удаляю @{username} из всех баз данных...\n")

    # --- statistics.db ---
    conn = sqlite3.connect(STATISTICS_DB)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM statistics WHERE username = ?", (username,))
    print(f"statistics:      удалено строк: {cursor.rowcount}")

    cursor.execute("DELETE FROM admin_settings WHERE username = ?", (username,))
    print(f"admin_settings:  удалено строк: {cursor.rowcount}")

    conn.commit()
    conn.close()

    # --- messages.db ---
    conn = sqlite3.connect(MESSAGES_DB)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM messages WHERE username = ?", (username,))
    msg_count = cursor.fetchone()[0]

    if msg_count > 0:
        confirm = input(f"\nВ messages.db найдено {msg_count} сообщений от @{username}. Удалить? [y/N] ")
        if confirm.strip().lower() == 'y':
            cursor.execute("DELETE FROM messages WHERE username = ?", (username,))
            print(f"messages:        удалено строк: {cursor.rowcount}")
        else:
            print("messages:        пропущено")
    else:
        print("messages:        сообщений не найдено")

    conn.commit()
    conn.close()

    print(f"\n✓ @{username} удалён из баз данных.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python scripts/remove_admin.py <username>")
        print("Пример:        python scripts/remove_admin.py gesegnet_she")
        sys.exit(1)

    remove_admin(sys.argv[1])
