import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import image_dedup as dedup

with patch.dict(os.environ, {'BOT_TOKEN': '123456:local-test-token', 'CHANNEL_ID': '-100123'}), patch('dotenv.load_dotenv'):
    import bot
    import msgs
    import adminstat

from aiogram import types


def meme(text='THIS IS THE ORIGINAL MEME'):
    rng = np.random.default_rng(41)
    # Coarse photographic texture, rather than independent noise at each pixel.
    pixels = rng.integers(30, 200, (60, 90, 3), dtype=np.uint8)
    image = Image.fromarray(pixels).resize((360, 240), Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 360, 85), fill='white')
    draw.text((5, 12), text, font=ImageFont.load_default(size=22), fill='black')
    draw.ellipse((70, 100, 230, 235), fill=(180, 80, 60))
    draw.rectangle((250, 120, 320, 200), fill=(10, 190, 130))
    return image


def encoded(image, format='PNG', **kwargs):
    with io.BytesIO() as output:
        image.save(output, format=format, **kwargs)
        return output.getvalue()


def message(mid=1, uid='file-one', user=2, caption=None):
    return types.Message(
        message_id=mid, date=datetime.now(timezone.utc), caption=caption,
        chat=types.Chat(id=user, type='private'),
        from_user=types.User(id=user, is_bot=False, first_name='Test', username=f'admin{user}'),
        photo=[types.PhotoSize(file_id='download-id', file_unique_id=uid, width=360, height=240)],
    )


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, 'messages.db')
        for patcher in (
            patch.object(msgs, 'MESSAGES_DB', self.db),
            patch.object(msgs.adminstat, 'get_weekday_mode', return_value=None),
            patch.object(msgs.adminstat, 'get_media_group_mode', return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        msgs.init_messages_db()
        self.settings = dedup.Settings()
        self.fingerprint = dedup.prepare(encoded(meme()), self.settings)
        self.history = dedup.ImageHistory(self.db, self.settings)

    def save(self, mid=1, uid='file-one', user=2, fingerprint=None):
        return msgs.save_message_to_db(message(mid, uid, user), (uid, fingerprint or self.fingerprint))

    def test_recompression_resize_and_color_cast_match(self):
        self.save()
        original = meme()
        tinted = np.asarray(original).astype(float) * np.array([1.04, 0.98, 0.96])
        variants = [
            encoded(original, 'JPEG', quality=62),
            encoded(original.resize((210, 140)), 'JPEG', quality=72),
            encoded(Image.fromarray(np.clip(tinted, 0, 255).astype('uint8'))),
        ]
        for data in variants:
            with self.subTest(size=len(data)):
                with patch.object(dedup, 'similarity', wraps=dedup.similarity) as compare:
                    result = self.history.find(fingerprint=dedup.prepare(data, self.settings))
                    compare.assert_called()
                self.assertIsNotNone(result)
                self.assertEqual(result['user_id'], 2)

    def test_changed_caption_inside_image_is_not_a_duplicate(self):
        self.save()
        other = dedup.prepare(encoded(meme('A COMPLETELY DIFFERENT JOKE')), self.settings)
        self.assertLess(dedup.similarity(self.fingerprint.rgb, other.rgb), self.settings.similarity)
        self.assertIsNone(self.history.find(fingerprint=other))

    def test_raw_rgb_is_exactly_49152_bytes_and_no_files_created(self):
        self.save()
        self.assertEqual(os.listdir(self.temp.name), ['messages.db'])
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute('SELECT content_hash,image_rgb FROM messages').fetchone()
            self.assertTrue(row[0].startswith('image:v1:'))
            self.assertEqual(len(row[1]), 49152)
            self.assertEqual(row[1], self.fingerprint.rgb)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('messages',)])

    def test_clearposted_removes_hash_and_pixels(self):
        self.save()
        with patch.dict(os.environ, {'CHANNEL_ID': '-100999'}):
            msgs.update_message_posted(1, 2, 99)
        result = dedup.ImageHistory(self.db).find(unique_id='file-one')
        self.assertTrue(result['posted'])
        self.assertEqual(result['publication_channel_id'], -100999)
        msgs.clear_posted_messages()
        self.assertIsNone(self.history.find(unique_id='file-one'))

    def test_removing_queue_item_removes_pixels_and_reuses_pages(self):
        self.save()
        msgs.clear_queued_messages(2, [(1, 2)])
        self.assertIsNone(self.history.find(unique_id='file-one'))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertGreater(conn.execute('PRAGMA freelist_count').fetchone()[0], 0)
        self.save(mid=2)
        self.assertIsNotNone(self.history.find(unique_id='file-one'))

    def test_invalid_fingerprint_rolls_back_queue_insert(self):
        with self.assertRaises(dedup.ImageCheckError):
            self.save(fingerprint=replace(self.fingerprint, rgb=b'invalid'))
        self.assertEqual(msgs.load_all_messages(), [])

    def test_generic_save_preserves_fingerprint_and_rgb(self):
        self.save()
        posts = msgs.load_messages()
        self.assertEqual(posts[0]['content_hash'], self.fingerprint.content_hash)
        msgs.save_messages(posts)
        result = self.history.find(unique_id='file-one')
        self.assertEqual(result['image_rgb'], self.fingerprint.rgb)

    def test_non_images_keep_null_hash(self):
        text = message().model_copy(update={'photo': None, 'text': 'a new post'})
        result = msgs.save_message_to_db(text)
        self.assertIsNone(result['content_hash'])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT content_hash,image_rgb,file_unique_id FROM messages').fetchone(), (None, None, None))

    def test_migration_is_repeatable_and_old_posts_remain_null(self):
        # Start from a pre-feature database, then apply the normal startup migration twice.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE messages')
            conn.execute('''CREATE TABLE messages (message_id INTEGER,chat_id INTEGER,username TEXT,
                user_id INTEGER,current_message_id INTEGER,posted BOOLEAN DEFAULT FALSE,
                is_forwarded_from_channel BOOLEAN DEFAULT FALSE,views INTEGER DEFAULT 0,
                reactions INTEGER DEFAULT 0,PRIMARY KEY(message_id,chat_id))''')
            conn.execute('INSERT INTO messages(message_id,chat_id) VALUES (10,20)')
        msgs.init_messages_db()
        msgs.init_messages_db()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT message_id,content_hash,image_rgb FROM messages').fetchone(), (10, None, None))

    def test_limits_invalid_files_and_animations(self):
        animation = encoded(meme(), 'PNG', save_all=True, append_images=[meme('SECOND FRAME')])
        for data, settings in [
            (b'not an image', self.settings),
            (encoded(meme()), replace(self.settings, max_pixels=10)),
            (encoded(meme()), replace(self.settings, download_bytes=10)),
            (animation, self.settings),
        ]:
            with self.assertRaises(dedup.ImageCheckError):
                dedup.prepare(data, settings)
        with dedup.BoundedDownload(3) as output:
            with self.assertRaises(dedup.ImageCheckError):
                output.write(b'1234')

    def test_exif_orientation_and_transparency(self):
        original = meme()
        exif = Image.Exif()
        exif[274] = 6
        rotated = original.transpose(Image.Transpose.ROTATE_90)
        transformed = dedup.prepare(encoded(rotated, 'JPEG', quality=95, exif=exif), self.settings)
        self.assertGreater(dedup.similarity(self.fingerprint.rgb, transformed.rgb), 0.92)
        clear = Image.new('RGBA', (128, 128), (255, 0, 0, 0))
        transparent = dedup.prepare(encoded(clear), self.settings)
        self.assertEqual(transparent.rgb, b'\xff' * dedup.RGB_SIZE)

    def log_events(self, output):
        return [json.loads(record.getMessage().removeprefix('image_dedup ')) for record in output.records]

    def test_logs_distinguish_telegram_id_from_normalized_pixel_match(self):
        self.save()
        with self.assertLogs('image_dedup', level='INFO') as output:
            self.history.find(unique_id='file-one')
            self.history.find(fingerprint=self.fingerprint)
        events = self.log_events(output)
        self.assertEqual([event['reason'] for event in events],
                         ['file_unique_id_match', 'normalized_pixel_hash_match'])
        self.assertEqual(events[0]['match_message_id'], 1)
        self.assertEqual(events[0]['match_chat_id'], 2)

    def test_logs_include_ssim_components_threshold_and_distances(self):
        self.save()
        query = dedup.prepare(encoded(meme(), 'JPEG', quality=62), self.settings)
        with self.assertLogs('image_dedup', level='INFO') as output:
            self.assertIsNotNone(self.history.find(fingerprint=query))
        events = self.log_events(output)
        candidate = next(e for e in events if e['stage'] == 'candidates')
        self.assertEqual(candidate['gate'], 'AND')
        self.assertEqual(candidate['both_passed'], 1)
        comparison = next(e for e in events if e['stage'] == 'ssim')
        self.assertIn('phash_distance', comparison)
        self.assertIn('dhash_distance', comparison)
        self.assertIn('ssim_minimum', comparison)
        self.assertAlmostEqual(comparison['ssim_score'],
                               .75*comparison['ssim_mean'] + .25*comparison['ssim_p10'])
        self.assertEqual(comparison['ssim_threshold'], .92)
        self.assertEqual(events[-1]['reason'], 'ssim_match')

    def test_logs_explain_no_candidate_and_ssim_rejection(self):
        self.save()
        query = replace(self.fingerprint, content_hash='image:v1:different',
                        phash=self.fingerprint.phash ^ ((1 << 64)-1))
        with self.assertLogs('image_dedup', level='INFO') as output:
            self.assertIsNone(self.history.find(fingerprint=query))
        events = self.log_events(output)
        candidate = next(e for e in events if e['stage'] == 'candidates')
        self.assertEqual(candidate['nearest']['phash_distance'], 64)
        self.assertEqual(candidate['both_passed'], 0)
        self.assertEqual(events[-1]['reason'], 'no_candidates')
        query = dedup.prepare(encoded(meme(), 'JPEG', quality=62), self.settings)
        history = dedup.ImageHistory(self.db, replace(self.settings, similarity=1))
        with self.assertLogs('image_dedup', level='INFO') as output:
            self.assertIsNone(history.find(fingerprint=query))
        self.assertEqual(self.log_events(output)[-1]['reason'], 'ssim_below_threshold')


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, 'messages.db')
        for patcher in (
            patch.object(msgs, 'MESSAGES_DB', self.db),
            patch.object(adminstat, 'STATISTICS_DB', os.path.join(self.temp.name, 'statistics.db')),
            patch.object(bot, 'get_admin_ids', return_value=[1, 2, 3]),
            patch.object(bot, 'get_admin_uns', return_value=['admin1', 'admin2', 'admin3']),
            patch.object(msgs.adminstat, 'get_weekday_mode', return_value=None),
            patch.object(msgs.adminstat, 'get_media_group_mode', return_value=False),
            patch.object(types.Message, 'answer', new_callable=AsyncMock),
            patch.object(bot.bot, 'download', new_callable=AsyncMock),
        ):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == 'download':
                self.download = value
            if patcher.attribute == 'answer':
                self.answer = value
        bot.image_check_lock = asyncio.Lock()
        self.download_data = encoded(meme())
        self.buffers = []
        self.sequence = 0

        async def download(*args, destination, **kwargs):
            self.buffers.append(destination)
            destination.write(self.download_data)
        self.download.side_effect = download

    async def send(self, msg):
        return await bot.save_checked_image(msg, msg.photo[-1])

    async def command(self, text, user=1):
        self.sequence += 1
        command = message(user=user).model_copy(update={'photo': None, 'text': text})
        await bot.dp.feed_update(bot.bot, types.Update(update_id=self.sequence, message=command))

    async def test_file_unique_id_rejected_before_download_even_with_new_caption(self):
        self.assertIsNotNone(await self.send(message(caption='old caption')))
        self.download.reset_mock()
        self.assertIsNone(await self.send(message(mid=2, user=3, caption='new caption')))
        self.download.assert_not_awaited()
        self.assertIn('@admin2', self.answer.call_args.args[0])
        self.assertTrue(all(buf.closed for buf in self.buffers))

    async def test_decision_logs_have_message_context_and_no_captions_or_download_ids(self):
        with self.assertLogs('image_dedup', level='INFO') as output:
            await self.send(message(caption='PRIVATE_CAPTION_NEVER_LOGGED'))
            await self.send(message(mid=2, user=3))
        events = [json.loads(record.getMessage().removeprefix('image_dedup ')) for record in output.records]
        decision = events[-1]
        self.assertEqual(decision['stage'], 'decision')
        self.assertEqual(decision['outcome'], 'duplicate')
        self.assertEqual(decision['reason'], 'file_unique_id_match')
        self.assertEqual(decision['message_id'], 2)
        self.assertEqual(decision['user_id'], 3)
        self.assertEqual(decision['match_message_id'], 1)
        self.assertNotIn('PRIVATE_CAPTION_NEVER_LOGGED', str(output.output))
        self.assertNotIn('download-id', str(output.output))

    async def test_concurrent_uploads_only_enqueue_one(self):
        results = await asyncio.gather(self.send(message()), self.send(message(mid=2, uid='different-id', user=3)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(msgs.load_messages()), 1)

    async def test_new_file_id_and_new_telegram_caption_still_rejected(self):
        await self.send(message(caption='first caption'))
        self.assertIsNone(await self.send(message(mid=2, uid='reuploaded', user=3, caption='new caption')))
        self.assertEqual(self.download.await_count, 2)
        self.assertEqual(len(msgs.load_messages()), 1)

    async def test_chief_allowed_by_default_and_others_compare_against_his_posts(self):
        self.assertIsNotNone(await self.send(message(user=1)))
        self.download.reset_mock()
        self.assertIsNotNone(await self.send(message(mid=2, user=1)))
        self.download.assert_not_awaited()
        self.assertIsNone(await self.send(message(mid=3, user=2)))
        self.assertEqual(len(msgs.load_messages()), 2)
        self.assertTrue(all(m['content_hash'] is not None for m in msgs.load_messages()))

    async def test_toggle_permission_is_persistent_and_does_not_reset_settings(self):
        await self.command('/duplicates @ADMIN2')
        self.assertTrue(adminstat.duplicates_allowed(2, 1))
        adminstat.set_media_group_mode(2, False)
        adminstat.set_weekday_mode(2, 'monday')
        self.assertTrue(adminstat.duplicates_allowed(2, 1))
        self.assertIsNotNone(await self.send(message()))
        self.assertIsNotNone(await self.send(message(mid=2)))
        await self.command('/duplicates @admin2')
        self.assertFalse(adminstat.duplicates_allowed(2, 1))
        self.assertIsNone(await self.send(message(mid=3)))
        self.assertIn('сообщение №1', self.answer.call_args.args[0])
        with closing(sqlite3.connect(adminstat.STATISTICS_DB)) as conn:
            self.assertEqual(conn.execute('SELECT media_group_mode,weekday FROM admin_settings WHERE username=?', (2,)).fetchone(), (0, 'monday'))

    async def test_only_chief_can_toggle_and_target_must_be_admin(self):
        await self.command('/duplicates @admin3', user=2)
        self.assertFalse(adminstat.duplicates_allowed(3, 1))
        for text in ['/duplicates @unknown', '/duplicates @', '/duplicates @admin2 extra']:
            await self.command(text)
            self.assertFalse(adminstat.duplicates_allowed(2, 1))

    async def test_chief_can_toggle_his_own_default(self):
        await self.send(message(user=1))
        await self.command('/duplicates @admin1')
        self.assertFalse(adminstat.duplicates_allowed(1, 1))
        self.assertIsNone(await self.send(message(mid=2, user=1)))
        await self.command('/duplicates @admin1')
        self.assertTrue(adminstat.duplicates_allowed(1, 1))
        self.assertIsNotNone(await self.send(message(mid=3, user=1)))

    async def test_notice_for_own_queue_and_published_post(self):
        await self.send(message())
        await self.send(message(mid=2))
        self.assertIn('сообщение №1', self.answer.call_args.args[0])
        with patch.dict(os.environ, {'CHANNEL_ID': '-100987'}):
            msgs.update_message_posted(1, 2, 777)
        await self.send(message(mid=3, user=3))
        self.assertIn('https://t.me/c/987/777', self.answer.call_args.args[0])

    async def test_download_error_does_not_enqueue_or_leave_open_buffer(self):
        async def fail(*args, destination, **kwargs):
            self.buffers.append(destination)
            destination.write(b'partial')
            raise OSError('offline')
        self.download.side_effect = fail
        self.assertIsNone(await self.send(message()))
        self.assertEqual(msgs.load_messages(), [])
        self.assertTrue(self.buffers[0].closed)

    async def test_image_document_is_checked_and_other_content_unchanged(self):
        await self.send(message())
        document = message(mid=2, user=3).model_copy(update={
            'photo': None,
            'document': types.Document(file_id='document-id', file_unique_id='different-id', mime_type='image/png'),
        })
        await bot.handle_source_message(document)
        self.assertEqual(len(msgs.load_messages()), 1)
        text = message(mid=3, user=3).model_copy(update={'photo': None, 'text': 'a normal text post'})
        self.download.reset_mock()
        await bot.handle_source_message(text)
        self.download.assert_not_awaited()
        self.assertEqual(len(msgs.load_messages()), 2)
        self.assertIsNone(msgs.load_messages()[-1]['content_hash'])


if __name__ == '__main__':
    unittest.main()
