import os
import random
import importlib
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError
from aiogram.filters import Command
from aiohttp import ClientConnectionError, ClientPayloadError
from telethon import TelegramClient
import asyncio
import ssl
import certifi
from dataclasses import dataclass
from html import escape
from time import monotonic

import msgs
import image_dedup
import admin_utils
import timezone
from posting import (
    forward_saved_message,
    new_message_event,
    periodic_post,
    post_random,
    publication_lock,
)
from adminstat import (
    get_admin_uns,
    get_admin_ids,
    get_admin_id_by_un,
    load_stat,
    export_admin_stat_csv,
    load_top_posts,
    export_top_posts_csv,
    render_best_admins_image,
    drop_legacy_statistics_table,
    duplicates_allowed,
    toggle_duplicates,
    init_admin_settings,
    get_media_group_mode,
    get_weekday_mode,
    set_media_group_mode,
    set_weekday_mode,
)

ssl_context = ssl.create_default_context(cafile=certifi.where())

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
logger = logging.getLogger(__name__)

load_dotenv(override=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_CHAT_ID = int(os.getenv('CHANNEL_ID'))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CLEAR_CONFIRMATION = "да, я подтверждаю"
CLEAR_CONFIRMATION_TTL = 120
CLEAR_EXPIRED_MESSAGE = "Время подтверждения истекло. Очистка отменена, бот работает в обычном режиме."


@dataclass
class PendingClear:
    target_id: int
    target_name: str
    message_keys: list
    expires_at: float
    request_message_id: int
    timeout_task: asyncio.Task | None = None


pending_clears = {}
image_check_lock = asyncio.Lock()
mode_pin_lock = asyncio.Lock()
IMAGE_DOWNLOAD_TIMEOUT = 30
IMAGE_DOWNLOAD_RETRY_DELAYS = (1, 2, 4)
IMAGE_DOWNLOAD_TRANSIENT_ERRORS = (
    TelegramNetworkError, TelegramServerError, ClientConnectionError,
    ClientPayloadError, asyncio.TimeoutError,
)


def duplicate_image_notice(match, actor_id):
    author = '@' + match['username'] if match['username'] else str(match['user_id'])
    if match['posted']:
        channel = str(match['publication_channel_id'] or CHANNEL_CHAT_ID)
        target = channel[4:] if channel.startswith('-100') else channel.lstrip('-')
        location = f"https://t.me/c/{target}/{match['current_message_id']}"
        return f'Эта картинка уже опубликована в канале. Повтор не добавлен в отложку.\n{location}'
    if match['user_id'] == actor_id:
        return f"Эта картинка уже есть в твоей отложке — сообщение №{match['message_id']}. Повтор не сохранён."
    return f"Эта картинка уже в отложке у {author}. Повтор не добавлен."


async def download_image(photo, settings, history):
    if photo.file_size and photo.file_size > settings.download_bytes:
        raise image_dedup.ImageCheckError('Картинка превышает лимит загрузки')
    for attempt, delay in enumerate((*IMAGE_DOWNLOAD_RETRY_DELAYS, None), start=1):
        try:
            # A failed transfer may leave partial bytes in the buffer.
            with image_dedup.BoundedDownload(settings.download_bytes) as output:
                await asyncio.wait_for(
                    bot.download(photo.file_id, destination=output),
                    timeout=IMAGE_DOWNLOAD_TIMEOUT,
                )
                return output.getvalue()
        except IMAGE_DOWNLOAD_TRANSIENT_ERRORS as exc:
            if delay is None:
                raise
            history.log('download', level=logging.WARNING, outcome='retry',
                        error_type=type(exc).__name__, attempt=attempt, retry_in_seconds=delay)
            await asyncio.sleep(delay)


async def save_checked_image(message, photo):
    # Serialize check + save across all admins, including concurrent album updates.
    async with image_check_lock:
        context = {'chat_id': message.chat.id, 'message_id': message.message_id,
                   'user_id': message.from_user.id}
        history = image_dedup.ImageHistory(msgs.MESSAGES_DB, image_dedup.Settings(), context=context)
        history.log('start')
        check_incomplete = False

        def warn_skipped(stage, exc):
            nonlocal check_incomplete
            check_incomplete = True
            details = {'reason': str(exc)} if isinstance(exc, image_dedup.ImageCheckError) else {}
            history.log(stage, level=logging.WARNING, outcome='skipped', error_type=type(exc).__name__, **details)

        match = None
        try:
            await asyncio.to_thread(msgs.init_messages_db)
            match = await asyncio.to_thread(history.find, unique_id=photo.file_unique_id)
        except Exception as exc:
            warn_skipped('file_unique_id', exc)

        allowed = None
        try:
            allowed = await asyncio.to_thread(duplicates_allowed, message.from_user.id, get_admin_ids()[0])
            history.log('permission', duplicates_allowed=allowed)
        except Exception as exc:
            warn_skipped('permission', exc)

        fingerprint = None
        reused_rgb = False
        if match is None or allowed is not False:
            if match is not None and match['image_rgb'] is not None:
                try:
                    fingerprint = image_dedup.Fingerprint(
                        match['content_hash'], int(match['image_phash'], 16),
                        int(match['image_dhash'], 16), match['image_rgb'],
                    )
                    reused_rgb = True
                except (TypeError, ValueError) as exc:
                    warn_skipped('reuse_rgb', exc)

            if fingerprint is None:
                stage = 'settings'
                try:
                    settings = image_dedup.Settings.from_env()
                    history.settings = settings
                    stage = 'download'
                    data = await download_image(photo, settings, history)
                    stage = 'normalized'
                    fingerprint = await asyncio.to_thread(image_dedup.prepare, data, settings)
                    history.log('normalized', download_bytes=len(data), rgb_bytes=len(fingerprint.rgb),
                                width=128, height=128, content_hash=fingerprint.content_hash,
                                phash=f'{fingerprint.phash:016x}', dhash=f'{fingerprint.dhash:016x}')
                except Exception as exc:
                    warn_skipped(stage, exc)

            if match is None and fingerprint is not None and allowed is not True:
                try:
                    match = await asyncio.to_thread(history.find, fingerprint=fingerprint)
                except Exception as exc:
                    warn_skipped('visual_match', exc)

        if match is not None and allowed is False:
            history.log('decision', outcome='duplicate', reason=history.reason, **image_dedup.match_reference(match))
            await message.answer(duplicate_image_notice(match, message.from_user.id))
            return None

        try:
            saved = await asyncio.to_thread(
                msgs.save_message_to_db, message, (photo.file_unique_id, fingerprint)
            )
        except Exception as exc:
            if fingerprint is None:
                raise
            # A failed fingerprint write rolls back the insert. Retry with the Telegram ID alone.
            warn_skipped('store_fingerprint', exc)
            reused_rgb = False
            saved = await asyncio.to_thread(msgs.save_message_to_db, message, (photo.file_unique_id, None))

        reason = 'duplicates_allowed' if allowed else history.reason
        if check_incomplete:
            reason = 'check_incomplete'
        history.log('decision', outcome='saved', reason=reason, content_hash=saved['content_hash'],
                    reused_rgb=reused_rgb)
        return saved


def clear_request_key(message):
    return (message.chat.id, message.from_user.id)


def take_pending_clear(key):
    request = pending_clears.pop(key, None)
    if request is not None and request.timeout_task is not None:
        request.timeout_task.cancel()
    return request


async def expire_clear_request(message, request):
    await asyncio.sleep(max(0, request.expires_at - monotonic()))
    key = clear_request_key(message)
    # Старый таймер не должен отменить новый запрос или прислать лишнее уведомление.
    if pending_clears.get(key) is not request:
        return
    pending_clears.pop(key)
    try:
        await message.answer(CLEAR_EXPIRED_MESSAGE)
    except Exception:
        logger.exception("Не удалось отправить уведомление об истечении подтверждения actor=%s", key[1])


def can_clear_queue(actor_id, target_id):
    ids = get_admin_ids()
    return actor_id in ids and target_id in ids and (actor_id == target_id or actor_id == ids[0])


# Регистрируется до обработчика контента: подтверждение не должно стать постом.
@dp.message(lambda message: message.text and message.text.strip().casefold() == CLEAR_CONFIRMATION)
async def confirm_clear(message: types.Message):
    key = clear_request_key(message)
    request = take_pending_clear(key)
    if request is None:
        await message.answer("Нет запроса на очистку. Сначала используйте /clear.")
        return
    if message.message_id <= request.request_message_id or monotonic() >= request.expires_at:
        await message.answer("Подтверждение устарело. Повторите /clear.")
        return

    try:
        async with publication_lock:
            if monotonic() >= request.expires_at:
                await message.answer("Подтверждение устарело. Повторите /clear.")
                return
            if not can_clear_queue(message.from_user.id, request.target_id):
                await message.answer("Очистка отменена: права администраторов изменились.")
                return
            deleted = msgs.clear_queued_messages(request.target_id, request.message_keys)
        logger.info("Очистка очереди: actor=%s target=%s deleted=%s", message.from_user.id, request.target_id, deleted)
        await message.answer(
            f"Отложка {request.target_name} очищена. Удалено сообщений: {deleted}.\n"
            "Опубликованные посты и сообщения, добавленные после запроса, сохранены."
        )
    except Exception:
        logger.exception("Ошибка очистки очереди target=%s", request.target_id)
        await message.answer("Не удалось очистить отложку. Повторите /clear.")


@dp.message(Command("cancel"))
async def cancel_clear(message: types.Message):
    request = take_pending_clear(clear_request_key(message))
    await message.answer("Запрос на очистку отменён." if request else "Нет запроса на очистку.")


def admin_required(func):
    async def wrapper(message):
        if message.from_user.id not in get_admin_ids():
            await message.answer(f"Недостаточно прав для совершения команды.")
            return
        return await func(message)
    return wrapper


def general_admin_required(func):
    async def wrapper(message):
        ids = get_admin_ids()
        if message.from_user.id not in ids:
            await message.answer(f"Недостаточно прав для совершения команды")
            return
        elif ids and message.from_user.id != ids[0]:
            await message.answer("Для этой команды требуются права главного админа")
            return
        return await func(message)
    return wrapper


async def ensure_admin_ids():
    load_dotenv(override=True)
    import os
    uns = get_admin_uns()
    if not uns:
        return
    current = os.getenv('ADMIN_IDS', '').strip()
    if current:
        return
    
    try:
        resolved = await admin_utils.resolve_usernames_to_ids(uns)
    except Exception as e:
        logger.error(f"Failed to resolve admin usernames to IDs: {e}")
        return
    with open('.env', 'r') as f:
        lines = f.readlines()
    with open('.env', 'w') as f:
        written = False
        for line in lines:
            if line.startswith('ADMIN_IDS'):
                f.write(f"ADMIN_IDS = {','.join(map(str, resolved))}\n")
                written = True
            else:
                f.write(line)
        if not written:
            f.write(f"ADMIN_IDS = {','.join(map(str, resolved))}\n")


async def normalize_bot_mappings():
    load_dotenv(override=True)
    import os
    BOT_MAPPINGS = os.getenv('BOT_MAPPINGS', '')
    if not BOT_MAPPINGS:
        return
    parts = []
    changed = False
    for mapping in BOT_MAPPINGS.split(','):
        if ':' not in mapping:
            continue
        token, tag = mapping.rsplit(':', 1)
        original = tag
        try:
            tag = str(int(tag))
        except ValueError:
            try:
                tag = str(await admin_utils.resolve_username_to_id(tag.lstrip('@')))
            except Exception:
                tag = original
        if tag != original:
            changed = True
        parts.append(f"{token}:{tag}")
    if changed:
        with open('.env', 'r') as f:
            lines = f.readlines()
        with open('.env', 'w') as f:
            wrote = False
            for line in lines:
                if line.startswith('BOT_MAPPINGS'):
                    f.write(f"BOT_MAPPINGS = {','.join(parts)}\n")
                    wrote = True
                else:
                    f.write(line)
            if not wrote:
                f.write(f"BOT_MAPPINGS = {','.join(parts)}\n")


@dp.message(lambda message: message.photo or message.document or message.video or message.audio or message.sticker or message.poll or (message.text and not message.text.startswith('/')))
@admin_required
async def handle_source_message(message: types.Message):
    photo = message.photo[-1] if message.photo else None
    if photo is None and message.document and (message.document.mime_type or '').startswith('image/'):
        # Animations remain outside the static-image checker.
        if message.document.mime_type != 'image/gif':
            photo = message.document
    if photo is not None:
        message_data = await save_checked_image(message, photo)
        if message_data is None:
            return
    else:
        message_data = msgs.save_message_to_db(message)
    weekday = message_data.get('weekday')
    weekday_note = ""
    if weekday:
        day_ru = msgs.WEEKDAY_NAMES_RU_ACCUSATIVE.get(weekday, weekday)
        weekday_note = f" на {day_ru}"
    new_message_event.set()
    await message.answer(f"Сообщение сохранено в базе данных: ID {message_data['message_id']} от {message_data['username']}{weekday_note}")
    logger.info(f"Сообщение сохранено в базе данных: ID {message_data['message_id']} от {message_data['username']}{weekday_note}")


@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer(
        "Привет! Я бот для автоматического постинга в Telegram-канал.\n\n"
        "Присылай мне фото, видео или текст — я сохраню их в очередь и опубликую в канал по расписанию.\n\n"
        "Используй /help чтобы увидеть доступные команды."
    )


MODE_LINE_PREFIX = 'Режим: '


def mode_summary(admin_id):
    media = '🗂️ медиа группами' if get_media_group_mode(admin_id) else '🖼️ медиа по отдельности'
    weekday = get_weekday_mode(admin_id)
    day = msgs.WEEKDAY_NAMES_RU.get(weekday, weekday) if weekday else 'любой день'
    return f'{MODE_LINE_PREFIX}{media} ·  день публикации: 📅{day}'


@dp.message(Command("help"))
@admin_required
async def help_command(message: types.Message):
    logger.info("Справка запрошена пользователем ID %s", message.from_user.id)
    ids = get_admin_ids()
    is_chief = bool(ids) and message.from_user.id == ids[0]
    heading = (
        "Вот список моих команд:\n" if is_chief else "Команды для администраторов:\n\n"
    )
    common_commands = (
        "/help — список команд\n"
        "/stat — статистика по всем админам за всё время (по умолчанию)\n"
        "/stat <b>&lt;N&gt;</b> — статистика за последние N дней (например, <code>/stat 7</code>)\n"
        "/posts — топ-10 постов по отклику за всё время\n"
        "/posts <b>&lt;N&gt;</b> — топ-10 постов за последние N дней\n"
        "/posts <b>&lt;@username&gt;</b> — топ-10 постов конкретного админа\n"
        "/posts <b>&lt;@username&gt;</b> <b>&lt;N&gt;</b> — топ-10 постов админа за N дней\n"
        "/best — топ-5 админов по отклику (картинка)\n"
        "/best <b>&lt;N&gt;</b> — топ-5 за последние N дней\n"
        "/messages — выгрузить сохранённые сообщения в CSV\n"
        "/delmsg <b>&lt;message_id&gt;</b> — удалить своё сообщение из базы по ID\n"
        "/clear — очистить свою отложку с подтверждением\n"
        "/cancel — отменить запрос на очистку\n"
        "/group <b>&lt;on/off&gt;</b> — медиагруппы (сохранять альбом целиком или по отдельности)\n"
        "/weekday — показать текущий день публикации\n"
        "/weekday <b>&lt;день&gt;</b> — публиковать новые посты только в этот день (monday…sunday)\n"
        "/weekday off — публиковать новые посты в любой день\n"
    )
    chief_commands = (
        "\nКоманды главного администратора:\n"
        "/post — опубликовать случайный пост прямо сейчас\n"
        "/post <b>&lt;message_id&gt;</b> — опубликовать пост по ID\n"
        "/post <b>&lt;@username&gt;</b> — опубликовать случайный пост админа\n"
        "/post <b>&lt;@username&gt;</b> <b>&lt;message_id&gt;</b> — опубликовать пост админа по ID\n"
        "/settime <b>&lt;HH:MM&gt;</b> <b>&lt;HH:MM&gt;</b> — установить время начала и конца постинга\n"
        "/setinterval <b>&lt;seconds&gt;</b> — установить интервал постинга в секундах\n"
        "/resetstattime <b>&lt;days&gt;</b> — сохранить интервал сброса статистики (автосброс сейчас отключён)\n"
        "/config — текущие настройки\n"
        "/clear <b>&lt;@username&gt;</b> — очистить отложку админа с подтверждением\n"
        "/clearposted — удалить опубликованные сообщения из базы\n"
        "/collectstats — собрать просмотры и реакции\n"
        "/duplicates <b>&lt;@username&gt;</b> — переключить разрешение повторных картинок для админа\n"
        "/addadm <b>&lt;@username&gt;</b> — добавить админа\n"
        "/deladm <b>&lt;@username&gt;</b> — удалить админа\n"
        "/addbot <b>&lt;BOT_TOKEN&gt;</b> <b>&lt;@username&gt;</b> — добавить именного бота для пользователя\n"
        "/deletebot <b>&lt;@username&gt;</b> — удалить именного бота пользователя\n"
    )
    summary = escape(mode_summary(message.from_user.id), quote=False)
    await message.answer(
        f'{summary}\n\n{heading}{common_commands}' + (chief_commands if is_chief else ''), parse_mode='HTML'
    )


@dp.message(Command("post"))
@general_admin_required
async def post_message(message: types.Message):
    args = message.text.split()
    logger.info(f"Команда /post использована пользователем @{message.from_user.username}, аргументы: {args[1:]}")
    try:
        # /post — случайный пост без проверки интервала
        if len(args) == 1:
            success = await post_random()
            if success:
                await message.answer("Пост опубликован")
            else:
                await message.answer("Не удалось опубликовать пост — нет доступных сообщений")
            return

        # /post @username [<message_id>] — пост от конкретного админа
        if args[1].startswith('@'):
            username = args[1].lstrip('@')
            admin_id = get_admin_id_by_un(username)
            all_msgs = msgs.load_messages()
            user_msgs = [m for m in all_msgs if m.get('user_id') == admin_id or m.get('username') == username]
            if not user_msgs:
                await message.answer(f"У @{username} нет сообщений в очереди")
                return

            # /post @username <message_id> — конкретный пост этого админа (для проверки именного бота)
            if len(args) >= 3:
                target_id = int(args[2])
                if not any(m['message_id'] == target_id for m in user_msgs):
                    await message.answer(f"У @{username} нет сообщения {target_id} в очереди")
                    return
                success, reason = await forward_saved_message(target_id, CHANNEL_CHAT_ID)
                if success:
                    await message.answer(f"Сообщение {target_id} от @{username} опубликовано")
                else:
                    await message.answer(f"Не удалось опубликовать сообщение {target_id} от @{username}: {reason}")
                return

            chosen = random.choice(user_msgs)
            success, reason = await forward_saved_message(chosen['message_id'], CHANNEL_CHAT_ID)
            if success:
                await message.answer(f"Пост от @{username} опубликован")
            else:
                await message.answer(f"Не удалось опубликовать пост от @{username}: {reason}")
            return

        # /post <message_id> — конкретный пост по ID
        message_id = int(args[1])
        success, reason = await forward_saved_message(message_id, CHANNEL_CHAT_ID)
        if success:
            await message.answer(f"Сообщение {message_id} переслано в канал")
        else:
            await message.answer(f"Не удалось переслать сообщение {message_id}: {reason}")

    except ValueError:
        await message.answer("Используйте:\n/post — случайный пост прямо сейчас\n/post @username — пост от конкретного админа\n/post @username <id> — конкретный пост этого админа\n/post <id> — пост по ID")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

@dp.message(Command("delmsg"))
@admin_required
async def delete_message(message: types.Message):
    logger.info(f"Команда /delmsg использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer(
                "Используйте: /delmsg <message_id>\n\n"
                "Пример: /delmsg 123\n"
                "Список сообщений: /info"
            )
            return
        try:
            msg_id = int(args[1])
            if msgs.clear_message(msg_id, message.from_user.id) > 0:
                logger.info(f"Сообщение {msg_id} удалено!")
                await message.answer(f"Сообщение {msg_id} удалено!")
            else: 
                logger.warning(f"Сообщение {msg_id} удалено!")
                await message.answer(f"Не удалось удалить сообщение")
        except Exception as e:
            logger.error(f"Вводимый параметр должен быть числом! {e}")
            await message.answer(f"Вводимый параметр должен быть числом!")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("settime"))
@general_admin_required
async def set_time(message: types.Message):
    logger.info(f"Команда /settime использована пользователем @{message.from_user.username} с аргументами {message.text.split()[1:] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Используйте: /settime <start_time> <end_time>\nПример: /settime 09:00 18:00")
            return
        llimit = args[1].split(":")
        ulimit = args[2].split(":")
        if len(llimit) != 2 or len(ulimit) != 2:
            await message.answer("Неверный формат времени. Используйте HH:MM")
            return
        start_hour, start_min = int(llimit[0]), int(llimit[1])
        end_hour, end_min = int(ulimit[0]), int(ulimit[1])
        if not (0 <= start_hour <= 23 and 0 <= start_min <= 59 and 0 <= end_hour <= 23 and 0 <= end_min <= 59):
            await message.answer("Время должно быть в диапазоне 00:00 - 23:59")
            return
        start_time = start_hour * 60 + start_min
        end_time = end_hour * 60 + end_min
        if start_time >= end_time:
            await message.answer("Время начала должно быть меньше времени конца")
            return
        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('START_HOUR'):
                    f.write(f"START_HOUR = {start_hour}\n")
                elif line.startswith('START_MINUTE'):
                    f.write(f"START_MINUTE = {start_min}\n")
                elif line.startswith('END_HOUR'):
                    f.write(f"END_HOUR = {end_hour}\n")
                elif line.startswith('END_MINUTE'):
                    f.write(f"END_MINUTE = {end_min}\n")
                else:
                    f.write(line)
        await message.answer(f"Время постинга установлено: {start_hour:02d}:{start_min:02d} - {end_hour:02d}:{end_min:02d}")
    except ValueError:
        await message.answer("Неверный формат времени. Используйте HH:MM")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("setinterval"))
@general_admin_required
async def set_interval(message: types.Message):
    logger.info(f"Команда /setinterval использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /setinterval <seconds>\nПример: /setinterval 7200")
            return
        interval = int(args[1])
        if interval <= 0:
            await message.answer("Интервал должен быть положительным числом")
            return
        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('POSTING_INTERVAL'):
                    f.write(f'POSTING_INTERVAL = {interval}\n')
                else:
                    f.write(line)
        await message.answer(f"Интервал постинга установлен: {interval} секунд")
    except ValueError:
        await message.answer("Интервал должен быть целым числом")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("resetstattime"))
@general_admin_required
async def set_reset_stat_time(message: types.Message):
    logger.info(f"Команда /resetstattime использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /resetstattime <days>\nПример: /resetstattime 7")
            return
        days = int(args[1])
        if days <= 0:
            await message.answer("Интервал должен быть положительным числом")
            return
        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('RESET_INTERVAL_DAYS'):
                    f.write(f"RESET_INTERVAL_DAYS = {days}\n")
                else:
                    f.write(line)
        await message.answer(f"Интервал сброса статистики установлен: {days} дней")
    except ValueError:
        await message.answer("Интервал должен быть целым числом")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("stat"))
@admin_required
async def stat_command(message: types.Message):
    args = message.text.split()
    days = None

    if len(args) > 2:
        await message.answer("Используйте: /stat или /stat <days>\nПример: /stat 7")
        return

    if len(args) == 2:
        try:
            days = int(args[1])
            if days <= 0:
                await message.answer("Количество дней должно быть положительным числом")
                return
        except ValueError:
            await message.answer("Количество дней должно быть целым числом")
            return
    try:
        stat = load_stat(days=days)
        filename = export_admin_stat_csv(stat)
    except Exception as e:
        await logger.error(f"Код ошибки {e}")

    caption = "📊 Статистика по админам"
    if days is not None:
        caption += f" за последние {days} дн."

    await message.answer_document(
        types.FSInputFile(filename),
        caption=caption
    )

    os.remove(filename)


@dp.message(Command("config"))
@general_admin_required
async def config_command(message: types.Message):
    logger.info(f"Команда /config использована пользователем @{message.from_user.username}")
    import config
    import runtime
    importlib.reload(config)
    admins = get_admin_uns()
    from datetime import timedelta
    offset = config.TIMEZONE_OFFSET
    last_post_local = runtime.get_last_post_time() + timedelta(hours=offset)
    last_reset_local = runtime.get_last_reset_date() + timedelta(hours=offset)
    response = "Текущие настройки конфигурации:\n\n"
    response += f"Время начала постинга: {config.START_HOUR:02d}:{config.START_MINUTE:02d}\n"
    response += f"Время конца постинга: {config.END_HOUR:02d}:{config.END_MINUTE:02d}\n"
    response += f"Интервал постинга: {round(config.POSTING_INTERVAL)} секунд\n"
    response += f"Последний пост: {last_post_local.strftime('%Y-%m-%d %H:%M:%S')} (UTC+{offset})\n"
    response += f"Последний сброс статистики: {last_reset_local.strftime('%Y-%m-%d')} (UTC+{offset})\n"
    response += f"Интервал сброса статистики: {config.RESET_INTERVAL_DAYS} дней\n"
    response += f"Админы: {', '.join('@' + adm for adm in admins)}\n"
    response += f"Смещение часового пояса: {offset} часов\n"
    await message.answer(response)


@dp.message(Command("clear"))
@admin_required
async def clear_message(message: types.Message):
    logger.info(f"Команда /clear использована пользователем @{message.from_user.username}")
    key = clear_request_key(message)
    # Любой новый /clear заменяет предыдущий запрос, даже если аргументы ошибочны.
    take_pending_clear(key)
    try:
        args = message.text.split()
        if len(args) > 2:
            await message.answer("Используйте /clear или /clear @username (только главный админ).")
            return
        actor_id = message.from_user.id
        ids = get_admin_ids()
        names = get_admin_uns()
        target_id = actor_id
        if len(args) == 2:
            if actor_id != ids[0]:
                await message.answer("Указывать username может только главный админ. Для своей отложки используйте /clear.")
                return
            username = args[1].lstrip('@').casefold()
            targets = [uid for name, uid in zip(names, ids) if name.casefold() == username and username]
            if len(targets) != 1:
                await message.answer("Админ с таким username не найден однозначно. Проверьте /config.")
                return
            target_id = targets[0]
        if not can_clear_queue(actor_id, target_id):
            await message.answer("Недостаточно прав для очистки этой отложки.")
            return
        target_name = next((f"@{name}" for name, uid in zip(names, ids) if uid == target_id and name), f"ID {target_id}")
        message_keys = msgs.get_queued_message_keys(target_id)
        if not message_keys:
            await message.answer(f"Отложка {target_name} уже пуста.")
            return
        request = PendingClear(target_id, target_name, message_keys, monotonic() + CLEAR_CONFIRMATION_TTL, message.message_id)
        pending_clears[key] = request
        request.timeout_task = asyncio.create_task(expire_clear_request(message, request))
        await message.answer(
            f"Очистить отложку {target_name}? В очереди сообщений: {len(message_keys)}.\n"
            "Опубликованные посты сохранятся. Добавленные после этого запроса сообщения тоже сохранятся.\n\n"
            f"В течение 2 минут напишите отдельным текстовым сообщением в этом чате:\n{CLEAR_CONFIRMATION}\n\n"
            "Отмена: /cancel."
        )
    except Exception:
        take_pending_clear(key)
        logger.exception("Ошибка запроса очистки очереди")
        await message.answer("Не удалось запросить очистку отложки. Повторите /clear.")


@dp.message(Command("clearposted"))
@general_admin_required
async def clear_posted(message: types.Message):
    logger.info(f"Команда /clearposted использована пользователем @{message.from_user.username}")
    try:
        msgs.clear_posted_messages()
        await message.answer("Опубликованные сообщения удалены из базы данных")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("collectstats"))
@general_admin_required
async def collectstats_command(message: types.Message):
    logger.info(f"Команда /collectstats использована пользователем @{message.from_user.username}")
    await message.answer("Запускаю сбор статистики...")
    try:
        await msgs.collect_message_stats()
        await message.answer("Готово. Смотри логи — если ошибок нет, всё работает")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("posts"))
@admin_required
async def posts_command(message: types.Message):
    logger.info(f"Команда /posts использована пользователем @{message.from_user.username}")
    args = message.text.split()
    days = None
    username = None

    for arg in args[1:]:
        arg_clean = arg.lstrip('@')
        try:
            n = int(arg_clean)
            if n <= 0:
                await message.answer("Количество дней должно быть положительным числом")
                return
            days = n
        except ValueError:
            username = arg_clean

    posts = load_top_posts(days=days, username=username)
    if not posts:
        await message.answer("Нет опубликованных постов за выбранный период")
        return

    filename = export_top_posts_csv(posts)
    caption = f"🏆 Топ-{len(posts)} постов"
    if username:
        caption += f" @{username}"
    if days:
        caption += f" за последние {days} дн."
    else:
        caption += " за всё время"

    await message.answer_document(types.FSInputFile(filename), caption=caption)
    os.remove(filename)

    # Сообщение со ссылками, отсортированными по убыванию вовлечённости
    posts_with_url = [p for p in posts if p.get('url')]
    if posts_with_url:
        header = caption + ":\n"
        lines = []
        for p in posts_with_url:
            views = p['views'] or 0
            reactions = p['reactions'] or 0
            lines.append(f"{reactions}❤️ {views}👁  {p['url']}")
        await message.answer(header + "\n".join(lines))


@dp.message(Command("best"))
@admin_required
async def best_command(message: types.Message):
    logger.info(f"Команда /best использована пользователем @{message.from_user.username}")
    args = message.text.split()
    days = None
    if len(args) == 2:
        try:
            days = int(args[1])
            if days <= 0:
                await message.answer("Количество дней должно быть положительным числом")
                return
        except ValueError:
            await message.answer("Используйте: /best или /best <N>\nПример: /best 7")
            return
    elif len(args) > 2:
        await message.answer("Используйте: /best или /best <N>\nПример: /best 7")
        return

    try:
        stat = load_stat(days=days)
        buf = render_best_admins_image(stat, days=days)
        caption = "Топ-5 админов по отклику"
        if days:
            caption += f" за последние {days} дн."
        await message.answer_photo(types.BufferedInputFile(buf.read(), filename="best.png"), caption=caption)
    except ValueError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("messages"))
@admin_required
async def messages_command(message: types.Message):
    logger.info(f"Команда /messages использована пользователем @{message.from_user.username}")
    stat = msgs.load_all_messages()
    filename = msgs.export_msgs_csv(stat)

    await message.answer_document(
        types.FSInputFile(filename),
        caption="📊 Статистика по сообщениям"
    )

    os.remove(filename)


@dp.message(Command("duplicates"))
@general_admin_required
async def duplicates_command(message: types.Message):
    args = message.text.split()
    if len(args) != 2 or not args[1].lstrip('@'):
        await message.answer('Используйте: /duplicates @username')
        return
    requested = args[1].lstrip('@').casefold()
    admins = list(zip(get_admin_uns(), get_admin_ids()))
    target = next(((name, uid) for name, uid in admins if name.casefold() == requested), None)
    if target is None:
        await message.answer('Этот пользователь не входит в список администраторов.')
        return
    name, user_id = target
    async with image_check_lock:
        allowed = await asyncio.to_thread(toggle_duplicates, user_id, get_admin_ids()[0])
    status = 'разрешены' if allowed else 'запрещены'
    await message.answer(f'Повторные картинки для @{name} теперь {status}.')


@dp.message(Command("addadm"))
@general_admin_required
async def add_admin(message: types.Message):
    logger.info(f"Команда /addadm использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        admin_uns = get_admin_uns()
        admin_ids = get_admin_ids()
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /addadm @<username>\nПример: /addadm @ivan")
            return

        new_us = args[1].lstrip('@')
        if new_us in admin_uns:
            await message.answer("Этот пользователь уже является админом.")
            return

        new_id = await admin_utils.resolve_username_to_id(new_us)
        admin_uns.append(new_us)
        admin_ids.append(new_id)

        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('ADMIN_UNS'):
                    f.write(f"ADMIN_UNS = {','.join(map(str, admin_uns))}\n")
                elif line.startswith('ADMIN_IDS'):
                    f.write(f"ADMIN_IDS = {','.join(map(str, admin_ids))}\n")
                else:
                    f.write(line)

        await message.answer(f"Новый админ добавлен: {new_us}")

    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("deladm"))
@general_admin_required
async def del_admin(message: types.Message):
    logger.info(f"Команда /deladm использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        load_dotenv(override=True)
        admin_uns = get_admin_uns()
        admin_ids = os.getenv('ADMIN_IDS', '').split(',')
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /deladm @<username>\nПример: /deladm @ivan")
            return

        username = args[1].lstrip('@')
        if username not in admin_uns:
            await message.answer("Этот пользователь уже отсутствует в списке админов.")
            return

        idx = admin_uns.index(username)
        admin_uns.pop(idx)
        if idx < len(admin_ids):
            admin_ids.pop(idx)

        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('ADMIN_UNS'):
                    f.write(f"ADMIN_UNS = {','.join(admin_uns)}\n")
                elif line.startswith('ADMIN_IDS'):
                    f.write(f"ADMIN_IDS = {','.join(admin_ids)}\n")
                else:
                    f.write(line)

        await message.answer(f"Пользователь @{username} лишён прав админа и удалён из статистики.")

    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("addbot"))
@general_admin_required
async def add_bot(message: types.Message):
    logger.info(f"Команда /addbot использована пользователем @{message.from_user.username} с аргументами {message.text.split()[1:] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Используйте: /addbot <BOT_TOKEN> <USER_TAG>\nПример: /addbot 123456:ABC-DEF... @ivan")
            return

        bot_token = args[1]
        user_tag = args[2].lstrip('@')
        try:
            user_id = await admin_utils.resolve_username_to_id(user_tag)
        except Exception as e:
            await message.answer(f"Не удалось определить ID пользователя {user_tag}: {e}")
            return

        BOT_MAPPINGS = os.getenv('BOT_MAPPINGS', '')
        bots = {}
        if BOT_MAPPINGS:
            for mapping in BOT_MAPPINGS.split(','):
                if ':' in mapping:
                    parts = mapping.split(':')
                    if len(parts) >= 2:
                        token = ':'.join(parts[:-1])
                        bots[parts[-1]] = token

        if str(user_id) in bots:
            await message.answer(f"Бот для пользователя {user_tag} уже существует.")
            return

        with open('.env', 'r') as f:
            lines = f.readlines()

        bot_mappings = []
        for line in lines:
            if line.startswith('BOT_MAPPINGS'):
                existing = line.split('=')[1].strip()
                if existing:
                    bot_mappings = existing.split(',')
                break
        else:
            lines.append('BOT_MAPPINGS =\n')

        bot_mappings.append(f'{bot_token}:{user_id}')

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('BOT_MAPPINGS'):
                    f.write(f'BOT_MAPPINGS = {",".join(bot_mappings)}\n')
                else:
                    f.write(line)

        await message.answer(f"Бот для пользователя {user_tag} добавлен.")

    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("deletebot"))
@general_admin_required
async def delete_bot(message: types.Message):
    logger.info(f"Команда /deletebot использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /deletebot <USER_TAG>\nПример: /deletebot @ivan")
            return

        user_tag = args[1].lstrip('@')
        try:
            user_id = await admin_utils.resolve_username_to_id(user_tag)
        except Exception:
            user_id = None

        BOT_MAPPINGS = os.getenv('BOT_MAPPINGS', '')
        bots = {}
        if BOT_MAPPINGS:
            for mapping in BOT_MAPPINGS.split(','):
                if ':' in mapping:
                    parts = mapping.split(':')
                    if len(parts) >= 2:
                        token = ':'.join(parts[:-1])
                        bots[parts[-1]] = token

        if str(user_id) not in bots:
            await message.answer(f"Бот для пользователя {user_tag} не найден.")
            return

        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('BOT_MAPPINGS'):
                    existing = line.split('=')[1].strip()
                    if existing:
                        mappings = [m for m in existing.split(',') if not m.endswith(f':{user_id}')]
                        f.write(f'BOT_MAPPINGS = {",".join(mappings)}\n')
                    else:
                        f.write('BOT_MAPPINGS =\n')
                else:
                    f.write(line)

        await message.answer(f"Бот для пользователя {user_tag} удалён.")

    except Exception as e:
        await message.answer(f"Ошибка: {e}")


VALID_WEEKDAYS = list(msgs.WEEKDAY_NAMES_RU.keys())


async def update_pinned_mode(chat_id, admin_id):
    pinned = (await bot.get_chat(chat_id)).pinned_message
    if not pinned or not pinned.text or not pinned.from_user or pinned.from_user.id != bot.id:
        return

    current = pinned.html_text
    first_line, newline, rest = current.partition('\n')
    summary = escape(mode_summary(admin_id), quote=False)
    if first_line == summary:
        return
    mode_prefixes = (f'{MODE_LINE_PREFIX}🗂️ медиа ', f'{MODE_LINE_PREFIX}🖼️ медиа ')
    if first_line.startswith(mode_prefixes) or first_line == 'Привет, готов к работе! :)':
        updated = summary + newline + rest
    else:
        updated = f'{summary}\n\n{current}'
    await bot.edit_message_text(
        chat_id=chat_id, message_id=pinned.message_id, text=updated, parse_mode='HTML',
        reply_markup=pinned.reply_markup, link_preview_options=pinned.link_preview_options,
    )


async def confirm_mode_change(message, text):
    chat_id = message.chat.id
    try:
        async with mode_pin_lock:
            await update_pinned_mode(chat_id, message.from_user.id)
    except TelegramAPIError as exc:
        logger.warning('Ошибка обновления закрепа настроек: chat_id=%s, %s', chat_id, exc)
    await message.answer(text)


@dp.message(Command("weekday"))
@admin_required
async def weekday_command(message: types.Message):
    args = message.text.split()
    username = message.from_user.username

    if len(args) == 1:
        current = get_weekday_mode(message.from_user.id)
        if current:
            day_ru = msgs.WEEKDAY_NAMES_RU.get(current, current)
            await message.answer(f"Твои новые посты выкладываются только в {day_ru}.\nЧтобы отключить: /weekday off")
        else:
            await message.answer("День недели не установлен — посты выкладываются в любой день.\nПример: /weekday sunday")
        return

    if len(args) != 2:
        await message.answer(
            "Используйте:\n"
            "/weekday — показать текущий режим\n"
            "/weekday <день> — публиковать новые посты только в этот день\n"
            "/weekday off — публиковать в любой день\n\n"
            "Дни: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
        )
        return

    arg = args[1].lower()

    if arg == 'off':
        set_weekday_mode(message.from_user.id, None)
        await confirm_mode_change(message, f"@{username}: новые посты будут выкладываться в любой день")
        return

    if arg not in VALID_WEEKDAYS:
        await message.answer(
            "Неверный день недели. Используйте:\n"
            "monday, tuesday, wednesday, thursday, friday, saturday, sunday"
        )
        return

    set_weekday_mode(message.from_user.id, arg)
    day_ru = msgs.WEEKDAY_NAMES_RU.get(arg, arg)
    await confirm_mode_change(
        message,
        f"@{username}: новые посты будут выкладываться только в {day_ru}."
    )


@dp.message(Command("group"))
@admin_required
async def group_command(message: types.Message):
    args = message.text.split()
    if len(args) != 2 or args[1].lower() not in ["on", "off"]:
        await message.answer("Используйте: /group on или /group off")
        return
    
    mode = args[1].lower() == "on"
    set_media_group_mode(message.from_user.id, mode)
    
    await confirm_mode_change(
        message,
        f"Режим сохранения медиа для @{message.from_user.username} установлен: {'группами' if mode else 'отдельно'}",
    )



async def main():
    msgs.init_messages_db()
    init_admin_settings()
    try:
        await ensure_admin_ids()
    except Exception as e:
        logger.error(f"Error ensuring admin ids: {e}")
    try:
        await normalize_bot_mappings()
    except Exception as e:
        logger.error(f"Error normalizing bot mappings: {e}")
    try:
        await msgs.update_user_ids()
    except Exception as e:
        logger.error(f"Error updating user IDs at database: {e}")
    try:
        drop_legacy_statistics_table()
    except Exception as e:
        logger.error(f"Error dropping legacy statistics table: {e}")

    asyncio.create_task(periodic_post())
    await dp.start_polling(bot)
    logger.info("Бот запущен")


if __name__ == "__main__":
    asyncio.run(main())
