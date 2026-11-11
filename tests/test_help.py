import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import types

with patch.dict(os.environ, {"BOT_TOKEN": "123456:local-test-token", "CHANNEL_ID": "-100123"}), patch("dotenv.load_dotenv"):
    import bot


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def request_help(self, command, user_id, username="chief", ids=None):
        message = types.Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=user_id, type="private"),
            from_user=types.User(id=user_id, is_bot=False, first_name="Test", username=username),
            text=command,
        )
        with patch.object(bot, "get_admin_ids", return_value=[1, 2] if ids is None else ids), patch.object(
            types.Message, "answer", new_callable=AsyncMock
        ) as answer:
            await bot.dp.feed_update(bot.bot, types.Update(update_id=1, message=message))
        answer.assert_awaited_once()
        return answer.call_args.args[0]

    async def test_regular_admin_sees_only_shared_commands_even_with_chief_username(self):
        text = await self.request_help("/help", 2)
        self.assertIn("/clear —", text)
        self.assertIn("/messages —", text)
        self.assertIn("/stat —", text)
        self.assertIn("/group ", text)
        for command in ("/clear <", "/duplicates", "/post ", "/settime", "/setinterval",
                        "/resetstattime", "/config", "/clearposted", "/collectstats",
                        "/addadm", "/deladm", "/addbot", "/deletebot"):
            with self.subTest(command=command):
                self.assertNotIn(command, text)

    async def test_chief_sees_full_help_without_username(self):
        text = await self.request_help("/help", 1, username=None)
        self.assertTrue(text.startswith("Привет, готов к работе! :)"))
        for command in ("/clear <", "/duplicates", "/post ", "/settime", "/setinterval",
                        "/resetstattime", "/config", "/clearposted", "/collectstats",
                        "/addadm", "/deladm", "/addbot", "/deletebot", "/stat", "/messages"):
            with self.subTest(command=command):
                self.assertIn(command, text)
        self.assertNotIn("/groups", text)
        self.assertLess(len(text), 4096)

    async def test_non_admin_cannot_read_help(self):
        for ids in ([1, 2], []):
            with self.subTest(ids=ids):
                text = await self.request_help("/help", 99, ids=ids)
                self.assertIn("Недостаточно прав", text)
                self.assertNotIn("/clear", text)

    async def test_chief_role_follows_current_id_order(self):
        text = await self.request_help("/help", 1, ids=[2, 1])
        self.assertNotIn("/duplicates", text)
        text = await self.request_help("/help", 2, ids=[2, 1])
        self.assertIn("/duplicates", text)


if __name__ == "__main__":
    unittest.main()
