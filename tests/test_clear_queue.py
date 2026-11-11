import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


# Import without production credentials or reading a deployment's .env.
with patch.dict(os.environ, {"BOT_TOKEN": "123456:local-test-token", "CHANNEL_ID": "-100123"}), patch("dotenv.load_dotenv"):
    import bot
    import msgs
    import posting

from aiogram import types


class ClearQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, "messages.db")
        self.ids = [1, 2, 3]
        for patcher in (
            patch.object(msgs, "MESSAGES_DB", self.db),
            patch.object(bot, "get_admin_ids", side_effect=lambda: self.ids[:]),
            patch.object(bot, "get_admin_uns", return_value=["chief", "alice", "bob"]),
            patch.object(bot, "monotonic", return_value=100),
            patch.object(types.Message, "answer", new_callable=AsyncMock),
        ):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == "answer":
                self.answer = value
        bot.pending_clears.clear()
        # Each test has its own event loop.
        bot.publication_lock = posting.publication_lock = asyncio.Lock()
        msgs.init_messages_db()
        self.add_message(10, 1)
        self.add_message(20, 2)
        self.add_message(21, 2, posted=True)
        self.add_message(30, 3)
        self.sequence = 0

    def add_message(self, message_id, user_id, posted=False, chat_id=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                'INSERT INTO messages (message_id, chat_id, user_id, posted) VALUES (?, ?, ?, ?)',
                (message_id, chat_id or user_id, user_id, posted),
            )

    def remaining(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT message_id, chat_id, posted FROM messages ORDER BY message_id, chat_id').fetchall()

    async def send(self, text, user_id=2, chat_id=None, message_id=None):
        self.sequence += 1
        message = types.Message(
            message_id=message_id if message_id is not None else self.sequence,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=chat_id or user_id, type="private"),
            from_user=types.User(id=user_id, is_bot=False, first_name="Test"),
            text=text,
        )
        await bot.dp.feed_update(bot.bot, types.Update(update_id=self.sequence, message=message))

    async def test_admin_clears_only_own_pending_messages(self):
        before = self.remaining()
        await self.send("/clear")
        self.assertEqual(self.remaining(), before)
        self.assertIn("@alice", self.answer.call_args.args[0])
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertEqual(self.remaining(), [(10, 1, 0), (21, 2, 1), (30, 3, 0)])
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertIn("Нет запроса", self.answer.call_args.args[0])

    async def test_chief_without_argument_clears_own_queue(self):
        await self.send("/clear", 1)
        await self.send(bot.CLEAR_CONFIRMATION, 1)
        self.assertEqual(self.remaining(), [(20, 2, 0), (21, 2, 1), (30, 3, 0)])

    async def test_chief_can_use_username_with_or_without_at(self):
        for argument in ("alice", "@ALICE"):
            with self.subTest(argument=argument):
                await self.send(f"/clear {argument}", 1)
                self.assertEqual(bot.pending_clears[(1, 1)].target_id, 2)
        await self.send(bot.CLEAR_CONFIRMATION, 1)
        self.assertNotIn((20, 2, 0), self.remaining())
        self.assertIn((21, 2, 1), self.remaining())

    async def test_admin_cannot_specify_target(self):
        before = self.remaining()
        for command in ("/clear bob", "/clear @bob", "/clear alice", "/clear @alice"):
            with self.subTest(command=command):
                await self.send(command)
                self.assertFalse(bot.pending_clears)
                self.assertIn("только главный админ", self.answer.call_args.args[0])
                await self.send(bot.CLEAR_CONFIRMATION)
                self.assertEqual(self.remaining(), before)

    async def test_timeout_sends_notice_without_any_further_message(self):
        before = self.remaining()
        with patch.object(bot, "CLEAR_CONFIRMATION_TTL", 0.01):
            await self.send("/clear")
        task = bot.pending_clears[(2, 2)].timeout_task
        self.answer.reset_mock()
        await asyncio.wait_for(task, timeout=1)
        self.answer.assert_awaited_once_with(bot.CLEAR_EXPIRED_MESSAGE)
        self.assertFalse(bot.pending_clears)
        self.assertEqual(self.remaining(), before)
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertEqual(self.remaining(), before)

    async def test_timeout_only_expires_the_request_in_its_chat(self):
        with patch.object(bot, "CLEAR_CONFIRMATION_TTL", 0.01):
            await self.send("/clear")
        task = bot.pending_clears[(2, 2)].timeout_task
        await self.send("/clear", 3)
        await asyncio.wait_for(task, timeout=1)
        self.assertNotIn((2, 2), bot.pending_clears)
        self.assertIn((3, 3), bot.pending_clears)

    async def test_confirm_and_cancel_stop_timeout_notice(self):
        for command in ("/cancel", bot.CLEAR_CONFIRMATION):
            with self.subTest(command=command):
                await self.send("/clear")
                task = bot.pending_clears[(2, 2)].timeout_task
                await self.send(command)
                self.answer.reset_mock()
                await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(task.cancelled())
                self.answer.assert_not_awaited()

    async def test_replacement_cancels_old_timeout(self):
        await self.send("/clear alice", 1)
        old_request = bot.pending_clears[(1, 1)]
        await self.send("/clear bob", 1)
        new_request = bot.pending_clears[(1, 1)]
        self.answer.reset_mock()
        await asyncio.gather(old_request.timeout_task, return_exceptions=True)
        self.assertTrue(old_request.timeout_task.cancelled())
        self.assertIs(bot.pending_clears[(1, 1)], new_request)
        self.answer.assert_not_awaited()

    async def test_non_admin_cannot_request_clear(self):
        await self.send("/clear", 99)
        self.assertFalse(bot.pending_clears)

    async def test_unknown_target_and_extra_arguments_rejected(self):
        for command in ("/clear @unknown", "/clear @alice extra", "/clear @"):
            await self.send(command, 1)
            self.assertFalse(bot.pending_clears)

    async def test_empty_queue_needs_no_confirmation(self):
        msgs.clear_queued_messages(2, msgs.get_queued_message_keys(2))
        await self.send("/clear")
        self.assertFalse(bot.pending_clears)
        self.assertIn("уже пуста", self.answer.call_args.args[0])

    async def test_confirmation_bound_to_user_and_chat(self):
        await self.send("/clear")
        await self.send(bot.CLEAR_CONFIRMATION, 3, 2)
        await self.send(bot.CLEAR_CONFIRMATION, 2, 200)
        self.assertIn((20, 2, 0), self.remaining())
        self.assertIn((2, 2), bot.pending_clears)
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertNotIn((20, 2, 0), self.remaining())

    async def test_expired_or_old_confirmation_does_not_delete(self):
        await self.send("/clear")
        with patch.object(bot, "monotonic", return_value=221):
            await self.send(bot.CLEAR_CONFIRMATION)
        self.assertIn((20, 2, 0), self.remaining())
        await self.send("/clear")
        await self.send(bot.CLEAR_CONFIRMATION, message_id=1)
        self.assertIn((20, 2, 0), self.remaining())

    async def test_permissions_rechecked_at_confirmation(self):
        await self.send("/clear alice", 1)
        self.ids = [3, 2, 1]  # The requester is no longer the chief.
        await self.send(bot.CLEAR_CONFIRMATION, 1)
        self.assertIn((20, 2, 0), self.remaining())
        self.ids = [1, 2, 3]
        await self.send("/clear")
        self.ids = [1, 3]  # The requester lost admin access entirely.
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertIn((20, 2, 0), self.remaining())

    async def test_cancel_and_new_request_invalidate_previous_target(self):
        await self.send("/clear alice", 1)
        await self.send("/clear bob", 1)
        await self.send(bot.CLEAR_CONFIRMATION, 1)
        self.assertIn((20, 2, 0), self.remaining())
        self.assertNotIn((30, 3, 0), self.remaining())
        await self.send("/clear")
        await self.send("/cancel")
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertIn((20, 2, 0), self.remaining())
        await self.send("/clear")
        await self.send("/clear bob")  # Even an invalid new request cancels the old one.
        self.assertFalse(bot.pending_clears)

    async def test_new_messages_and_same_message_id_in_other_chats_survive(self):
        await self.send("/clear")
        self.add_message(22, 2)
        self.add_message(20, 2, chat_id=200)
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertNotIn((20, 2, 0), self.remaining())
        self.assertIn((22, 2, 0), self.remaining())
        self.assertIn((20, 200, 0), self.remaining())

    async def test_post_published_while_waiting_is_preserved(self):
        await self.send("/clear")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE messages SET posted = TRUE WHERE message_id = 20')
        await self.send(bot.CLEAR_CONFIRMATION)
        self.assertIn((20, 2, 1), self.remaining())
        self.assertIn("Удалено сообщений: 0", self.answer.call_args.args[0])

    async def test_confirmation_waits_for_inflight_publication(self):
        await self.send("/clear")
        started, finish = asyncio.Event(), asyncio.Event()

        async def publish(*args):
            started.set()
            await finish.wait()
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE messages SET posted = TRUE WHERE message_id = 20')
            return True, None

        with patch.object(posting, "_forward_saved_message", side_effect=publish):
            publication = asyncio.create_task(posting.forward_saved_message(20, -100123))
            await started.wait()
            confirmation = asyncio.create_task(self.send(bot.CLEAR_CONFIRMATION))
            await asyncio.sleep(0)
            self.assertFalse(confirmation.done())
            finish.set()
            await asyncio.gather(publication, confirmation)
        self.assertIn((20, 2, 1), self.remaining())

    async def test_confirmation_is_never_saved_as_content(self):
        with patch.object(msgs, "save_message_to_db") as save:
            await self.send(bot.CLEAR_CONFIRMATION)
            await self.send("/clear")
            await self.send("  ДА, Я ПОДТВЕРЖДАЮ  ")
            save.assert_not_called()

    async def test_delete_failure_consumes_confirmation(self):
        await self.send("/clear")
        with patch.object(msgs, "clear_queued_messages", side_effect=sqlite3.OperationalError("test")):
            await self.send(bot.CLEAR_CONFIRMATION)
        self.assertFalse(bot.pending_clears)
        self.assertIn((20, 2, 0), self.remaining())

    async def test_database_failure_rolls_back_entire_clear(self):
        self.add_message(22, 2)
        before = self.remaining()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_second_delete BEFORE DELETE ON messages "
                "WHEN OLD.message_id = 22 BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            msgs.clear_queued_messages(2, [(20, 2), (22, 2)])
        self.assertEqual(self.remaining(), before)


if __name__ == "__main__":
    unittest.main()
