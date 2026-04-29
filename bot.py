import os
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from telethon import TelegramClient
import asyncio
import ssl
import certifi

import msgs
import admin_utils
from posting import (
    forward_saved_message,
    periodic_post
)
from adminstat import (
    get_admin_uns,
    get_admin_ids,
    load_stat,
    export_admin_stat_csv
)

ssl_context = ssl.create_default_context(cafile=certifi.where())

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
logger = logging.getLogger(__name__)

load_dotenv(override=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_CHAT_ID = int(os.getenv('CHANNEL_ID'))
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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


@dp.message(lambda message: message.photo or message.document or message.video or message.audio or message.sticker or (message.text and not message.text.startswith('/')))
@admin_required
async def handle_source_message(message: types.Message):
    message_data = msgs.save_message_to_db(message)
    await message.answer(f"Сообщение сохранено в базе данных: ID {message_data['message_id']} от {message_data['username']}")
    logger.info(f"Сообщение сохранено в базе данных: ID {message_data['message_id']} от {message_data['username']}")
    from posting import new_message_event
    new_message_event.set()


@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer("Привет, готов к работе! :)\n Используйте /help - там много интересного.")


@dp.message(Command("help"))
@admin_required
async def help_command(message: types.Message):
    logger.info(f"Команда /help использована пользователем @{message.from_user.username}")
    await message.answer(
        "Команды для администраторов:\n\n"
        "/help — список команд\n"
        "/stat — статистика по админам за всё время\n"
        "<b>/stat &lt;N&gt;</b> — статистика за последние N дней (например, <code>/stat 7</code>)\n"
        "/config — текущие настройки\n"
        "/messages — сохранённые сообщения\n"
        "/delmsg <b>&lt;message_id&gt;</b> — удалить сообщение из базы по ID\n"
        "/group <b>&lt;on/off&gt;</b> — медиагруппы (сохранять альбом целиком или по отдельности)\n",
        parse_mode="HTML"
    )


@dp.message(Command("post"))
@general_admin_required
async def post_message(message: types.Message):
    logger.info(f"Команда /post использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer(
                "Используйте: /post <message_id>\n\n"
                "Пример: /post 123\n"
                "Список сообщений: /info"
            )
            return

        message_id = int(args[1])
        success = await forward_saved_message(message_id, CHANNEL_CHAT_ID)

        if success:
            await message.answer(f"Сообщение {message_id} переслано в канал")
        else:
            await message.answer(f"Не удалось переслать сообщение {message_id}")

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
@admin_required
async def config_command(message: types.Message):
    logger.info(f"Команда /config использована пользователем @{message.from_user.username}")
    import config
    admins = get_admin_uns()
    response = "Текущие настройки конфигурации:\n\n"
    response += f"Время начала постинга: {config.START_HOUR:02d}:{config.START_MINUTE:02d}\n"
    response += f"Время конца постинга: {config.END_HOUR:02d}:{config.END_MINUTE:02d}\n"
    response += f"Интервал постинга: {round(config.POSTING_INTERVAL)} секунд\n"
    response += f"Последний пост: {config.LAST_TIME_POST}\n"
    response += f"Последний сброс статистики: {config.LAST_RESET_DATE}\n"
    response += f"Интервал сброса статистики: {config.RESET_INTERVAL_DAYS} дней\n"
    response += f"Админы: {', '.join('@' + adm for adm in admins)}\n"
    response += f"Смещение часового пояса: {config.TIMEZONE_OFFSET} часов\n"
    await message.answer(response)


@dp.message(Command("clear"))
@general_admin_required
async def clear_message(message: types.Message):
    logger.info(f"Команда /clear использована пользователем @{message.from_user.username}")
    try:
        msgs.clear_messages()
        await message.answer(f"База данных очищена")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


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

        new_us = args[1][1:]
        if new_us in admin_uns:
            await message.answer("Этот пользователь уже является админом.")
            return

        admin_uns.append(new_us)
        admin_ids = await admin_utils.resolve_usernames_to_ids(admin_uns)

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
        admins = get_admin_uns()
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /deladm @<username>\nПример: /deladm @ivan")
            return

        new_id = args[1][1:]
        if new_id in admins:
            admins.remove(new_id)
        else:
            await message.answer("Этот пользователь уже отсутствует в списке админов.")
            return


        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('ADMIN_UNS'):
                    f.write(f"ADMIN_UNS = {','.join(map(str, admins))}\n")
                else:
                    f.write(line)

        await message.answer(f"Пользователь {new_id} лишён прав админа.")

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


@dp.message(Command("group"))
@admin_required
async def group_command(message: types.Message):
    args = message.text.split()
    if len(args) != 2 or args[1].lower() not in ["on", "off"]:
        await message.answer("Используйте: /group on или /group off")
        return
    
    from adminstat import set_media_group_mode
    mode = args[1].lower() == "on"
    set_media_group_mode(message.from_user.id, mode)
    
    await message.answer(f"Режим сохранения медиа для @{message.from_user.username} установлен: {'группами' if mode else 'отдельно'}")


@dp.message(Command("tzset"))
@general_admin_required
async def tzset_command(message: types.Message):
    logger.info(f"Команда /tzset использована пользователем @{message.from_user.username} с аргументом {message.text.split()[1] if len(message.text.split()) > 1 else 'нет'}")
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Используйте: /tzset <offset>\nПример: /tzset 3")
            return
        offset = int(args[1])
        with open('.env', 'r') as f:
            lines = f.readlines()

        with open('.env', 'w') as f:
            for line in lines:
                if line.startswith('TIMEZONE_OFFSET'):
                    f.write(f"TIMEZONE_OFFSET = {offset}\n")
                else:
                    f.write(line)
        await message.answer(f"Смещение часового пояса установлено: {offset} часов")
    except ValueError:
        await message.answer("Смещение должно быть целым числом")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


async def main():
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

    asyncio.create_task(periodic_post())
    await dp.start_polling(bot)
    logger.info("Бот запущен")


if __name__ == "__main__":
    asyncio.run(main())
