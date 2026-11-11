import importlib
import logging
import os
import aiohttp
import ssl
import certifi
from datetime import datetime, time, timedelta
import asyncio
import random
import re
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from dotenv import load_dotenv
import msgs
import timezone
import runtime

WEEKDAY_EN = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
from adminstat import (
    get_admin_ids,
    get_admin_uns,
)

load_dotenv(override=True)
CHANNEL_CHAT_ID = int(os.getenv('CHANNEL_ID'))

ssl_context = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

new_message_event = asyncio.Event()
is_waiting_for_message = False


def _post_url(channel_id: int, message_id: int) -> str:
    cid = str(channel_id).replace('-100', '') if channel_id < 0 else str(channel_id)
    return f"https://t.me/c/{cid}/{message_id}"


def _classify_error_str(text: str) -> str:
    t = text.lower()
    if "blocked by the user" in t:
        return "Админ заблокировал бота"
    if "bot was kicked" in t or "chat not found" in t or "group chat was deleted" in t or "user is deactivated" in t:
        return "Чат с ботом недоступен (удалён или бот кикнут)"
    if "message to forward not found" in t or "no messages" in t:
        return "Сообщение удалено из чата с ботом"
    if "message to copy not found" in t:
        return "Оригинальное сообщение удалено из источника"
    if "not enough rights" in t or "have no rights" in t or "need administrator" in t:
        return "Нет прав для публикации в канале"
    if "flood" in t or "too many requests" in t:
        return "Превышен лимит запросов Telegram (flood)"
    return text or "Неизвестная ошибка"


def _classify_error(e: Exception) -> str:
    return _classify_error_str(str(e))


def _is_unrecoverable(reason: str) -> bool:
    """Ошибки, при которых сообщение никогда не получится опубликовать — удаляем из БД."""
    unrecoverable = [
        "Чат с ботом недоступен",
        "Сообщение удалено из чата с ботом",
        "Оригинальное сообщение удалено",
        "Админ заблокировал бота",
    ]
    return any(marker in reason for marker in unrecoverable)


# Three retries after the initial request, for each bot separately.
PUBLICATION_RETRY_DELAYS = (10, 20, 30)


class _NamedBotAPIError(Exception):
    def __init__(self, description, status, retry_after=0):
        super().__init__(description)
        self.status = status
        self.retry_after = retry_after


def _safe_error(error):
    # aiohttp errors can contain the request URL, including the bot token.
    if isinstance(error, asyncio.TimeoutError):
        return "Истекло время ожидания ответа Telegram"
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<скрыто>", str(error) or type(error).__name__)


def _retry_details(error):
    if isinstance(error, _NamedBotAPIError):
        return error.status == 429 or error.status >= 500, error.retry_after
    if isinstance(error, TelegramRetryAfter):
        return True, error.retry_after
    if isinstance(error, (TelegramNetworkError, TelegramServerError,
                          aiohttp.ClientConnectionError, aiohttp.ClientPayloadError,
                          asyncio.TimeoutError, ConnectionError)):
        return True, 0
    return False, 0


async def _publish_with_retries(send, message_id, bot_label):
    attempts = len(PUBLICATION_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        logger.info("Сообщение %s: %s, попытка публикации %s/%s",
                    message_id, bot_label, attempt, attempts)
        try:
            return await send()
        except Exception as error:
            retryable, retry_after = _retry_details(error)
            reason = _classify_error_str(_safe_error(error))
            if not retryable or attempt == attempts:
                logger.error("Сообщение %s: %s, попытка %s/%s не удалась: %s. %s",
                             message_id, bot_label, attempt, attempts, reason,
                             "Повторы исчерпаны" if retryable else "Ошибка не временная; повторы не выполняются")
                raise
            delay = max(PUBLICATION_RETRY_DELAYS[attempt - 1], retry_after)
            logger.warning("Сообщение %s: %s, временная ошибка на попытке %s/%s: %s. "
                           "Следующая попытка через %s с",
                           message_id, bot_label, attempt, attempts, reason, delay)
            await asyncio.sleep(delay)


async def _send_named_message(token, method, payload):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload, ssl=ssl_context,
        ) as response:
            # A gateway may return an HTML error page instead of Telegram JSON.
            if response.status == 429 or response.status >= 500:
                try:
                    data = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    data = {}
                raise _NamedBotAPIError(
                    data.get("description", f"Telegram HTTP {response.status}"),
                    response.status, data.get("parameters", {}).get("retry_after", 0),
                )
            data = await response.json()
            if not data.get("ok", False):
                raise _NamedBotAPIError(
                    data.get("description", f"Telegram HTTP {response.status}"),
                    data.get("error_code", response.status),
                    data.get("parameters", {}).get("retry_after", 0),
                )
            result = data["result"]
            return [item["message_id"] for item in result]


# Also held while a confirmed queue clear deletes rows.
publication_lock = asyncio.Lock()


async def forward_saved_message(target_message_id: int, target_chat_id: int):
    async with publication_lock:
        return await _forward_saved_message(target_message_id, target_chat_id)


async def _forward_saved_message(target_message_id: int, target_chat_id: int):

    messages = msgs.load_messages()
    BOT_MAPPINGS = os.getenv('BOT_MAPPINGS', '')
    mgid = ''
    bots = {}
    if BOT_MAPPINGS:
        for mapping in BOT_MAPPINGS.split(','):
            if ':' in mapping:
                parts = mapping.split(':')
                if len(parts) >= 2:
                    token = ':'.join(parts[:-1])
                    try:
                        user_id = int(parts[-1])
                    except ValueError:
                        user_id = parts[-1]
                    bots[user_id] = token

    for msg in messages:
        if msg['message_id'] == target_message_id:
            if msg['media_group']:
                msg_to_send = msgs.get_media_group_ids(msg['media_group'])
                mgid = f' - медиа группа {msg["media_group"]}'
            else:
                msg_to_send = [msg['message_id']]
            from bot import bot
            method = ("forwardMessages" if msg.get('is_forwarded_from_channel', True)
                      and not msg.get('is_poll') else "copyMessages")
            payload = {
                "chat_id": target_chat_id,
                "from_chat_id": msg['chat_id'],
                "message_ids": msg_to_send,
            }
            published_ids = None
            used_named_bot = False
            user_id = msg.get('user_id')
            if user_id in bots:
                label = f"именной бот админа @{msg.get('username') or user_id}"

                async def send_named():
                    return await _send_named_message(bots[user_id], method, payload)

                try:
                    published_ids = await _publish_with_retries(send_named, target_message_id, label)
                    used_named_bot = True
                except Exception as error:
                    logger.warning(
                        "Сообщение %s: привязка именного бота найдена, но публикация не удалась (%s). "
                        "Переходим на основного бота",
                        target_message_id, _classify_error_str(_safe_error(error)),
                    )
            else:
                logger.info("Сообщение %s: именной бот для админа %s не привязан; используем основного бота",
                            target_message_id, user_id)

            if published_ids is None:
                async def send_main():
                    send = bot.forward_messages if method == "forwardMessages" else bot.copy_messages
                    result = await send(**payload)
                    return [item.message_id for item in result]

                try:
                    published_ids = await _publish_with_retries(send_main, target_message_id, "основной бот")
                except Exception as error:
                    reason = _classify_error_str(_safe_error(error))
                    if _is_unrecoverable(reason):
                        msgs.clear_message(target_message_id, user_id)
                        logger.warning("Сообщение %s удалено из БД: %s", target_message_id, reason)
                    else:
                        logger.error("Сообщение %s не опубликовано и оставлено в очереди: %s",
                                     target_message_id, reason)
                    return False, reason

            # Publication is complete. A failed notification must never trigger a new send.
            for source_id, published_id in zip(msg_to_send, published_ids):
                msgs.update_message_posted(source_id, msg['chat_id'], published_id)
            post_link = _post_url(target_chat_id, published_ids[0])
            label = "именного бота" if used_named_bot else "основного бота"
            logger.info("Сообщение %s%s опубликовано через %s: %s", target_message_id, mgid, label, post_link)
            try:
                await bot.send_message(
                    msg['chat_id'],
                    f"Сообщение {target_message_id}{mgid} переслано в канал (через {label})\n{post_link}",
                )
            except Exception as error:
                logger.warning("Сообщение %s уже опубликовано (%s), но уведомление админу не доставлено: %s. "
                               "Повторная публикация не выполняется",
                               target_message_id, post_link, _safe_error(error))
            return True, None

    logger.warning(f"Сообщение {target_message_id} не найдено в базе")
    return False, "Сообщение не найдено в базе данных"


async def post(message_id: int):
    result = await forward_saved_message(message_id, CHANNEL_CHAT_ID)
    if result[0]:
        from datetime import timezone as dt_timezone
        runtime.set_last_post_time(datetime.now(dt_timezone.utc))
    return result


async def post_random():
    global is_waiting_for_message
    messages = msgs.load_messages()
    admins = get_admin_ids()

    if not messages:
        if not is_waiting_for_message:
            logger.warning("Нет постов для публикации. Ожидание нового сообщения...")
            is_waiting_for_message = True
        await new_message_event.wait()
        new_message_event.clear()
        is_waiting_for_message = False
        return await post_random()

    BOT_MAPPINGS = os.getenv('BOT_MAPPINGS', '')
    bots = {}
    if BOT_MAPPINGS:
        for mapping in BOT_MAPPINGS.split(','):
            if ':' in mapping:
                parts = mapping.split(':')
                if len(parts) >= 2:
                    token = ':'.join(parts[:-1])
                    try:
                        user_id = int(parts[-1])
                    except ValueError:
                        user_id = parts[-1]
                    bots[user_id] = token

    today_weekday = WEEKDAY_EN[timezone.tz_now().weekday()]

    shuffled_admins = admins[:]
    random.shuffle(shuffled_admins)

    for rand_adm in shuffled_admins:
        msg_from_adm = [msg for msg in messages if msg.get('user_id') == rand_adm]
        if not msg_from_adm:
            logger.warning(f"У админа {rand_adm} нет заготовленных постов, пропускаем")
            continue

        # Фильтруем по дню недели: берём посты без ограничения ИЛИ с нужным днём
        msg_for_today = [
            msg for msg in msg_from_adm
            if msg.get('weekday') is None or msg.get('weekday') == today_weekday
        ]
        if not msg_for_today:
            logger.info(f"У админа {rand_adm} нет постов для публикации сегодня ({today_weekday}), пропускаем")
            continue
        msg_from_adm = msg_for_today

        if rand_adm in bots:
            logger.info(f"Выбран админ для постинга: {rand_adm}. Используем именного бота")
        else:
            logger.info(f"Выбран админ для постинга: {rand_adm}. Используем основного бота")

        msg = random.choice(msg_from_adm)
        success, reason = await post(msg['message_id'])
        if success:
            return True
        else:
            logger.warning(f"Не удалось опубликовать пост {msg['message_id']} от админа {rand_adm}: {reason}. Пробуем следующего")
            continue

    logger.warning("Не удалось опубликовать ни одного поста от всех доступных админов")


async def periodic_post():
    while True:
        import config
        importlib.reload(config)
        now = timezone.tz_now().time()
        today = timezone.tz_now().date()
        # if (timezone.tz_now() - config.LAST_RESET_DATE).days >= config.RESET_INTERVAL_DAYS:
        #     from adminstat import reset_statistics
        #     from msgs import clear_posted_messages
        #     reset_statistics()
        #     clear_posted_messages()
        #     with open('.env', 'r') as f:
        #         lines = f.readlines()

        #     with open('.env', 'w') as f:
        #         for line in lines:
        #             if line.startswith('LAST_RESET_DATE'):
        #                 f.write(f"LAST_RESET_DATE = {today.isoformat()}\n")
        #             else:
        #                 f.write(line)
        start = time(config.START_HOUR, config.START_MINUTE)
        end = time(config.END_HOUR, config.END_MINUTE)
        if start <= end:
            in_window = start <= now <= end
        else:  # переход через полночь: например 07:00–02:00
            in_window = now >= start or now <= end

        if in_window:
            from datetime import timezone as dt_timezone
            elapsed = (datetime.now(dt_timezone.utc) - runtime.get_last_post_time()).total_seconds()
            if elapsed >= config.POSTING_INTERVAL:
                await post_random()
                await msgs.collect_message_stats()
            await asyncio.sleep(config.POSTING_INTERVAL)
        else:
            await asyncio.sleep(60)
