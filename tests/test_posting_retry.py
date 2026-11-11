import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

with patch.dict(os.environ, {"BOT_TOKEN": "123456:local-test-token", "CHANNEL_ID": "-100123"}), patch("dotenv.load_dotenv"):
    import bot
    import posting


class PublicationRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = dict(message_id=1264, chat_id=42, user_id=42,
                            username="nikslivkus", media_group=None,
                            is_forwarded_from_channel=False)
        self.events = []
        posting.publication_lock = asyncio.Lock()
        patches = (
            patch.dict(os.environ, {"BOT_MAPPINGS": "123456:named-token:42"}),
            patch.object(posting.msgs, "load_messages", return_value=[self.message]),
            patch.object(posting.msgs, "update_message_posted"),
            patch.object(posting.msgs, "clear_message"),
            patch.object(posting, "_send_named_message", new_callable=AsyncMock),
            patch.object(posting.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(bot.bot, "copy_messages", new_callable=AsyncMock),
            patch.object(bot.bot, "forward_messages", new_callable=AsyncMock),
            patch.object(bot.bot, "send_message", new_callable=AsyncMock),
        )
        values = []
        for patcher in patches:
            values.append(patcher.start())
            self.addCleanup(patcher.stop)
        (_, _, self.update, self.clear, self.named, self.sleep,
         self.copy, self.forward, self.notify) = values
        self.named.return_value = [8988]
        self.copy.return_value = [SimpleNamespace(message_id=8989)]
        self.forward.return_value = self.copy.return_value

    async def publish(self):
        return await posting.forward_saved_message(1264, -100123)

    async def test_named_recovers_after_network_errors_without_fallback(self):
        self.named.side_effect = [ConnectionResetError("Connection reset by peer"),
                                  asyncio.TimeoutError(), [8988]]
        self.assertEqual(await self.publish(), (True, None))
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [10, 20])
        self.copy.assert_not_awaited()
        self.update.assert_called_once_with(1264, 42, 8988)

    async def test_both_bots_exhaust_network_retries_in_order(self):
        async def named(*args):
            self.events.append("named")
            raise ConnectionResetError("Connection reset by peer")

        async def main(**kwargs):
            self.events.append("main")
            raise ConnectionResetError("Connection reset by peer")

        self.named.side_effect = named
        self.copy.side_effect = main
        with self.assertLogs(posting.logger, level="INFO") as logs:
            self.assertFalse((await self.publish())[0])
        self.assertEqual(self.events, ["named"] * 4 + ["main"] * 4)
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [10, 20, 30] * 2)
        self.update.assert_not_called()
        self.clear.assert_not_called()
        self.assertNotIn("/addbot", " ".join(logs.output))
        self.assertIn("оставлено в очереди", " ".join(logs.output))

    async def test_main_recovers_after_fallback(self):
        self.named.side_effect = ConnectionResetError("reset")
        self.copy.side_effect = [asyncio.TimeoutError(), self.copy.return_value]
        self.assertTrue((await self.publish())[0])
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [10, 20, 30, 10])
        self.update.assert_called_once_with(1264, 42, 8989)

    async def test_permanent_named_error_falls_back_without_wait(self):
        self.named.side_effect = posting._NamedBotAPIError("Unauthorized", 401)
        self.assertTrue((await self.publish())[0])
        self.sleep.assert_not_awaited()
        self.named.assert_awaited_once()
        self.clear.assert_not_called()

    async def test_no_mapping_uses_main_retries(self):
        with patch.dict(os.environ, {"BOT_MAPPINGS": ""}):
            self.copy.side_effect = [ConnectionResetError("reset"), self.copy.return_value]
            self.assertTrue((await self.publish())[0])
        self.named.assert_not_awaited()
        self.sleep.assert_awaited_once_with(10)

    async def test_flood_wait_respects_telegram_minimum(self):
        self.named.side_effect = [posting._NamedBotAPIError("Too Many Requests", 429, 45), [8988]]
        self.assertTrue((await self.publish())[0])
        self.sleep.assert_awaited_once_with(45)

    async def test_server_error_retries(self):
        self.named.side_effect = [posting._NamedBotAPIError("Bad Gateway", 502), [8988]]
        self.assertTrue((await self.publish())[0])
        self.sleep.assert_awaited_once_with(10)

    async def test_notification_failure_does_not_republish(self):
        for named in (True, False):
            with self.subTest(named=named), patch.dict(os.environ, {"BOT_MAPPINGS": "123456:token:42" if named else ""}):
                self.named.reset_mock()
                self.copy.reset_mock()
                self.notify.side_effect = ConnectionResetError("reset")
                self.assertTrue((await self.publish())[0])
                self.assertEqual(self.named.await_count, int(named))
                self.assertEqual(self.copy.await_count, int(not named))
        self.sleep.assert_not_awaited()

    async def test_forward_and_album_preserve_ids(self):
        self.message.update(is_forwarded_from_channel=True, media_group="album")
        self.named.return_value = [8988, 8989]
        with patch.object(posting.msgs, "get_media_group_ids", return_value=[1264, 1265]):
            self.assertTrue((await self.publish())[0])
        self.assertEqual(self.named.call_args.args[1], "forwardMessages")
        self.assertEqual(self.named.call_args.args[2]["message_ids"], [1264, 1265])
        self.assertEqual(self.update.call_count, 2)

    async def test_cancellation_does_not_fallback(self):
        self.named.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.publish()
        self.copy.assert_not_awaited()

    async def test_unrecoverable_main_error_clears_message(self):
        self.named.side_effect = posting._NamedBotAPIError("message to copy not found", 400)
        self.copy.side_effect = ValueError("message to copy not found")
        self.assertFalse((await self.publish())[0])
        self.clear.assert_called_once_with(1264, 42)
        self.sleep.assert_not_awaited()

    async def test_aiogram_temporary_errors_are_retried(self):
        from aiogram.methods import CopyMessages
        from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramRetryAfter
        method = CopyMessages(chat_id=-100123, from_chat_id=42, message_ids=[1264])
        errors = [TelegramNetworkError(method=method, message="reset"),
                  TelegramServerError(method=method, message="Bad Gateway"),
                  TelegramRetryAfter(method=method, message="Flood", retry_after=50)]
        with patch.dict(os.environ, {"BOT_MAPPINGS": ""}):
            self.copy.side_effect = errors + [self.copy.return_value]
            self.assertTrue((await self.publish())[0])
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [10, 20, 50])

    async def test_tokens_are_redacted(self):
        self.named.side_effect = ConnectionResetError("https://api.telegram.org/bot123456:secret-token/copyMessages reset")
        with self.assertLogs(posting.logger, level="INFO") as logs:
            await self.publish()
        self.assertNotIn("secret-token", " ".join(logs.output))


class NamedBotHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_json_response(self):
        for status, data in (
            (200, {"ok": True, "result": [{"message_id": 8988}]}),
            (400, {"ok": False, "error_code": 400, "description": "not enough rights"}),
            (200, {"ok": False, "error_code": 429, "parameters": {"retry_after": 40}}),
        ):
            with self.subTest(data=data):
                response = MagicMock(status=status)
                response.json = AsyncMock(return_value=data)
                request = MagicMock()
                request.__aenter__ = AsyncMock(return_value=response)
                session = MagicMock()
                session.post.return_value = request
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=session)
                with patch.object(posting.aiohttp, "ClientSession", return_value=context):
                    if data["ok"]:
                        self.assertEqual(await posting._send_named_message(
                            "token", "copyMessages", {"message_ids": [1]}), [8988])
                    else:
                        with self.assertRaises(posting._NamedBotAPIError) as raised:
                            await posting._send_named_message("token", "copyMessages", {"message_ids": [1]})
                        self.assertEqual(posting._retry_details(raised.exception),
                                         (data["error_code"] == 429,
                                          data.get("parameters", {}).get("retry_after", 0)))

    async def test_gateway_non_json_error_is_retryable(self):
        response = MagicMock(status=502)
        response.json = AsyncMock(side_effect=ValueError("HTML"))
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.post.return_value = request
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        with patch.object(posting.aiohttp, "ClientSession", return_value=context):
            with self.assertRaises(posting._NamedBotAPIError) as raised:
                await posting._send_named_message("token", "copyMessages", {"message_ids": [1]})
        self.assertEqual(posting._retry_details(raised.exception), (True, 0))


if __name__ == "__main__":
    unittest.main()
