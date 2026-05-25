"""
Выводит число подписчиков канала и выбирает случайного подписчика.

Запуск из корня проекта:
    python scripts/random_subscriber.py
"""

import asyncio
import os
import random
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch, User

load_dotenv()

API_ID = os.getenv("CORE_API_ID")
API_HASH = os.getenv("CORE_API_HASH")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))

if not API_ID or not API_HASH or not CHANNEL_ID:
    print("Ошибка: убедитесь что CORE_API_ID, CORE_API_HASH и CHANNEL_ID заданы в .env")
    sys.exit(1)


async def main():
    print("[1] Подключаемся к Telegram...")
    async with TelegramClient("session", int(API_ID), API_HASH) as client:
        print("[2] Подключение установлено. Получаем entity канала...")
        entity = await client.get_entity(CHANNEL_ID)
        print(f"[3] Entity получена: {entity}")

        # Число подписчиков из метаданных канала
        total = getattr(entity, "participants_count", None)
        print(f"[4] participants_count из entity: {total}")
        if total is None:
            print("[4a] participants_count не найден, запрашиваем GetFullChannelRequest...")
            from telethon.tl.functions.channels import GetFullChannelRequest
            full_channel = await client(GetFullChannelRequest(entity))
            total = full_channel.full_chat.participants_count
            print(f"[4b] participants_count из GetFullChannelRequest: {total}")

        print(f"Подписчиков в канале: {total}")

        # Получаем участников постранично (лимит API — 200 за запрос)
        participants = []
        offset = 0
        limit = 200
        print("[5] Начинаем загрузку участников...")
        while True:
            print(f"[5] GetParticipantsRequest offset={offset}...")
            result = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsSearch(""),
                offset=offset,
                limit=limit,
                hash=0,
            ))
            print(f"[5] Получено: {len(result.users)} пользователей")
            if not result.users:
                break
            participants.extend(result.users)
            offset += len(result.users)
            if len(result.users) < limit:
                break
        print(f"[6] Итого загружено профилей: {len(participants)}")

        # Фильтруем: только живые пользователи (не боты, не удалённые)
        real_users = [
            u for u in participants
            if isinstance(u, User) and not u.bot and not u.deleted
        ]

        if not real_users:
            print("Не удалось получить список подписчиков (возможно, канал публичный и API ограничен).")
            return

        winner = random.choice(real_users)
        username = f"@{winner.username}" if winner.username else "(нет username)"
        first = winner.first_name or ""
        last = winner.last_name or ""
        name = (first + " " + last).strip() or "(нет имени)"

        print(f"\n🎲 Случайный подписчик:")
        print(f"   ID:       {winner.id}")
        print(f"   Username: {username}")
        print(f"   Имя:      {name}")


if __name__ == "__main__":
    asyncio.run(main())
