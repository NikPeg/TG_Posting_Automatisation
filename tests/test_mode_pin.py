import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import types

with patch.dict(os.environ, {"BOT_TOKEN": "123456:local-test-token", "CHANNEL_ID": "-100123"}), patch("dotenv.load_dotenv"):
    import adminstat
    import bot


class ModePinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(adminstat, 'STATISTICS_DB', str(Path(tmp) / 'statistics.db')))
        stack.enter_context(patch.object(bot, 'get_admin_ids', return_value=[1, 2]))
        self.telegram = stack.enter_context(patch.object(bot, 'bot', id=123456))
        self.telegram.get_chat = AsyncMock(return_value=SimpleNamespace(pinned_message=None))
        self.telegram.edit_message_text = AsyncMock()
        self.answer = stack.enter_context(patch.object(types.Message, 'answer', new_callable=AsyncMock))

    async def command(self, text):
        message = types.Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=-100123, type='supergroup'),
            from_user=types.User(id=2, is_bot=False, first_name='Admin', username='admin'),
            text=text,
        )
        await bot.dp.feed_update(self.telegram, types.Update(update_id=1, message=message))

    def pin(self, header, *, author_id=123456, is_bot=True):
        body = '\n\n/help — список команд\n/group <on/off>'
        text = header + body
        offset = len(text[:text.index('<on/off>')].encode('utf-16-le')) // 2
        self.telegram.get_chat.return_value.pinned_message = types.Message(
            message_id=42,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=-100123, type='supergroup'),
            from_user=types.User(id=author_id, is_bot=is_bot, first_name='Author'),
            text=text,
            entities=[types.MessageEntity(type='bold', offset=offset, length=len('<on/off>'))],
        )

    async def test_group_edits_current_pin_and_preserves_help_formatting(self):
        adminstat.set_weekday_mode(2, 'friday')
        self.pin('Режим: 🖼️ медиа группами · 📅 день публикации — пятница')
        for mode, media in [('off', '🖼️ медиа по отдельности'), ('on', '🗂️ медиа группами'),
                            ('off', '🖼️ медиа по отдельности')]:
            await self.command(f'/group {mode}')
            header = f'Режим: {media} ·  день публикации: 📅пятница'
            self.telegram.get_chat.assert_awaited_with(-100123)
            self.telegram.edit_message_text.assert_awaited_with(
                chat_id=-100123, message_id=42, parse_mode='HTML', reply_markup=None, link_preview_options=None,
                text=f'{header}\n\n/help — список команд\n/group <b>&lt;on/off&gt;</b>',
            )
            self.pin(header)

    async def test_weekday_replaces_legacy_help_greeting(self):
        self.pin('Привет, готов к работе! :)')
        await self.command('/weekday monday')
        updated = self.telegram.edit_message_text.call_args.kwargs['text']
        self.assertEqual(
            updated.splitlines()[0], 'Режим: 🗂️ медиа группами ·  день публикации: 📅понедельник'
        )
        self.assertNotIn('Привет', updated)

    async def test_missing_pin_prompts_admin_to_pin_help(self):
        await self.command('/weekday monday')
        self.telegram.edit_message_text.assert_not_awaited()
        self.assertEqual(self.answer.await_count, 2)
        self.answer.assert_awaited_with(
            'В этой беседе пока нет закреплённого сообщения. '
            'Чтобы видеть текущий режим, отправьте /help и закрепите ответ бота.'
        )

    async def test_pins_from_other_authors_are_skipped(self):
        self.pin('Закреп админа', author_id=2, is_bot=False)
        await self.command('/group off')
        self.pin('Закреп именного бота', author_id=424242)
        await self.command('/weekday off')
        self.telegram.edit_message_text.assert_not_awaited()
        self.assertEqual(self.telegram.get_chat.await_count, 2)
        self.assertEqual(self.answer.await_count, 2)
        self.assertIsNone(adminstat.get_weekday_mode(2))
