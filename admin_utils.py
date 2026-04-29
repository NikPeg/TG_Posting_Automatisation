from telethon import TelegramClient
from telethon.tl.types import User
import os
from dotenv import load_dotenv

load_dotenv(override=True)
api_id = os.getenv('CORE_API_ID')
api_hash = os.getenv('CORE_API_HASH')

client = TelegramClient('admin_resolver', int(api_id), api_hash)

async def resolve_usernames_to_ids(usernames):
    await client.start()
    ids = []
    for username in usernames:
        entity = await client.get_entity(username)

        if not isinstance(entity, User):
            raise ValueError(f"{username} это не пользователь")

        ids.append(int(entity.id))
    return ids

async def resolve_username_to_id(username: str) -> int:
    await client.start()
    entity = await client.get_entity(username)

    if not isinstance(entity, User):
        raise ValueError("Это не пользователь")

    return entity.id

