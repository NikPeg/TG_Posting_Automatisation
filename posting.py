import importlib
import logging
import os
import aiohttp
import ssl
import certifi
from datetime import datetime, time, timedelta
import asyncio
import random
from dotenv import load_dotenv
import msgs
import timezone
from adminstat import (
    get_admin_ids,
    get_admin_uns,
    add_post_to_count,
    decrement_queued_to_count,
    get_meta,
    set_meta,
)

load_dotenv(override=True)
CHANNEL_CHAT_ID = int(os.getenv('CHANNEL_ID'))

ssl_context = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

new_message_event = asyncio.Event()
is_waiting_for_message = False


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


async def forward_saved_message(target_message_id: int, target_chat_id: int):

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
            if msg.get('user_id') not in bots:
                try:
                    from bot import bot
                    if msg.get('is_forwarded_from_channel', True):
                        forwarded_msg = await bot.forward_messages(
                            chat_id=target_chat_id,
                            from_chat_id=msg['chat_id'],
                            message_ids=msg_to_send
                        )
                    else:
                        forwarded_msg = await bot.copy_messages(
                            chat_id=target_chat_id,
                            from_chat_id=msg['chat_id'],
                            message_ids=msg_to_send
                        )

                    await bot.send_message(msg['chat_id'], f"Сообщение {target_message_id}{mgid} переслано в канал")
                    logger.info(f"Сообщение {target_message_id}{mgid} переслано в канал")
                    logger.info(forwarded_msg[0].message_id)
                    for msgts, fmsg in zip(msg_to_send, forwarded_msg):
                        msgs.update_message_posted(msgts, msg['chat_id'], fmsg.message_id)
                    return True, None

                except Exception as e:
                    reason = _classify_error(e)
                    logger.error(f"Ошибка при пересылке сообщения {target_message_id}: {reason}")
                    if "no messages" in str(e):
                        msgs.clear_message(target_message_id, msg.get('user_id'))
                    return False, reason
            else:
                other_bot_token = bots[msg.get('user_id')]
                namebot_api_url = f"https://api.telegram.org/bot{other_bot_token}"
                named_bot_ok = False
                named_bot_fail_reason = None
                try:
                    if msg.get('is_forwarded_from_channel', True):
                        method = "forwardMessages"
                    else:
                        method = "copyMessages"

                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{namebot_api_url}/{method}",
                            json={
                                "chat_id": target_chat_id,
                                "from_chat_id": msg['chat_id'],
                                "message_ids": msg_to_send,
                            },
                            ssl=ssl_context
                        ) as response:
                            data = await response.json()

                            if not data.get("ok", False):
                                description = data.get("description", "")
                                named_bot_fail_reason = _classify_error_str(description)
                                if "message" in description.lower():
                                    msgs.clear_message(target_message_id, msg.get('user_id'))
                            else:
                                forwarded_msg = data["result"]
                                named_bot_ok = True

                        if named_bot_ok:
                            genbot_api_url = f"https://api.telegram.org/bot{os.getenv('BOT_TOKEN')}"
                            async with session.post(
                                f"{genbot_api_url}/sendMessage",
                                json={
                                    "chat_id": msg['chat_id'],
                                    "text": f"Сообщение {msg['message_id']}{mgid} переслано в канал"
                                }
                            ) as response:
                                data = await response.json()
                                if not data.get("ok", False):
                                    logger.error(f"Ошибка при отправке уведомления: {data}")

                            logger.info(f"Сообщение {target_message_id}{mgid} отправлено ботом @{msg['username']}")
                            for msgts, fmsg in zip(msg_to_send, forwarded_msg):
                                msgs.update_message_posted(msgts, msg['chat_id'], fmsg['message_id'])
                            return True, None

                except Exception as e:
                    named_bot_fail_reason = _classify_error(e)

                logger.warning(
                    f"Именной бот @{msg['username']} недоступен ({named_bot_fail_reason}). "
                    f"Публикуем через основного бота. Добавьте именного бота командой /addbot"
                )
                try:
                    from bot import bot
                    if msg.get('is_forwarded_from_channel', True):
                        forwarded_msg = await bot.forward_messages(
                            chat_id=target_chat_id,
                            from_chat_id=msg['chat_id'],
                            message_ids=msg_to_send
                        )
                    else:
                        forwarded_msg = await bot.copy_messages(
                            chat_id=target_chat_id,
                            from_chat_id=msg['chat_id'],
                            message_ids=msg_to_send
                        )
                    await bot.send_message(msg['chat_id'], f"Сообщение {target_message_id}{mgid} переслано в канал (через основного бота)")
                    logger.info(f"Сообщение {target_message_id}{mgid} переслано через основного бота")
                    for msgts, fmsg in zip(msg_to_send, forwarded_msg):
                        msgs.update_message_posted(msgts, msg['chat_id'], fmsg.message_id)
                    return True, None
                except Exception as e:
                    reason = _classify_error(e)
                    logger.error(f"Fallback через основного бота тоже не удался для сообщения {target_message_id}: {reason}")
                    return False, reason

    logger.warning(f"Сообщение {target_message_id} не найдено в базе")
    return False, "Сообщение не найдено в базе данных"


async def post(message_id: int):
    result = await forward_saved_message(message_id, CHANNEL_CHAT_ID)
    if result[0]:
        set_meta('last_time_post', timezone.tz_now().isoformat())
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
                    username = parts[-1]
                    bots[username] = token

    shuffled_admins = admins[:]
    random.shuffle(shuffled_admins)

    for rand_adm in shuffled_admins:
        msg_from_adm = [msg for msg in messages if msg.get('user_id') == rand_adm]
        if not msg_from_adm:
            logger.warning(f"У админа {rand_adm} нет заготовленных постов, пропускаем")
            continue

        if rand_adm in bots:
            logger.info(f"Выбран админ для постинга: {rand_adm}. Используем именного бота")
        else:
            logger.info(f"Выбран админ для постинга: {rand_adm}. Используем основного бота")

        msg = random.choice(msg_from_adm)
        success, reason = await post(msg['message_id'])
        if success:
            add_post_to_count(msg['username'])
            decrement_queued_to_count(msg['username'])
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
            raw = get_meta('last_time_post')
            if raw:
                from datetime import timezone as dt_timezone
                last_post = datetime.fromisoformat(raw)
                if last_post.tzinfo is None:
                    last_post = last_post.replace(tzinfo=dt_timezone.utc)
                elapsed = (timezone.tz_now() - last_post).total_seconds()
            else:
                elapsed = config.POSTING_INTERVAL  # первый запуск — постим сразу

            if elapsed >= config.POSTING_INTERVAL:
                await post_random()
                await msgs.collect_message_stats()
            await asyncio.sleep(config.POSTING_INTERVAL)
        else:
            await asyncio.sleep(60)
