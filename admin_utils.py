from telethon import TelegramClient
from telethon.tl.types import User
import os
from dotenv import load_dotenv


def _make_client():
    load_dotenv(override=True)
    api_id = os.getenv('CORE_API_ID')
    api_hash = os.getenv('CORE_API_HASH')
    if not api_id or not api_hash:
        raise RuntimeError("CORE_API_ID or CORE_API_HASH not set in .env")
    proxy = _parse_proxy()
    return TelegramClient('session', int(api_id), api_hash, proxy=proxy)


def _parse_proxy():
    """Парсит PROXY_URL в формат для Telethon: (socks.SOCKS5, host, port)."""
    proxy_url = os.getenv('PROXY_URL', '')
    if not proxy_url:
        return None
    try:
        import socks
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
        if 'socks5' in scheme:
            return (socks.SOCKS5, host, port)
        elif 'socks4' in scheme:
            return (socks.SOCKS4, host, port)
        elif 'http' in scheme:
            return (socks.HTTP, host, port)
    except Exception:
        pass
    return None


async def resolve_usernames_to_ids(usernames):
    async with _make_client() as client:
        ids = []
        for username in usernames:
            entity = await client.get_entity(username)
            if not isinstance(entity, User):
                raise ValueError(f"{username} это не пользователь")
            ids.append(int(entity.id))
        return ids


async def resolve_username_to_id(username: str) -> int:
    async with _make_client() as client:
        entity = await client.get_entity(username)
        if not isinstance(entity, User):
            raise ValueError("Это не пользователь")
        return entity.id
