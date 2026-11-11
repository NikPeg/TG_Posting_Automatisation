"""Image fingerprints stored with messages; no originals or temporary files on disk.

Normalization, hash gates and block SSIM follow TeleBOYANkeeper's approach.
See THIRD_PARTY_NOTICES.md. Fingerprint versions are local to this implementation.
"""
import io
import hashlib
import os
import sqlite3
import heapq
import warnings
from contextlib import closing
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


class ImageCheckError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    download_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 20_000_000
    phash_distance: int = 10
    dhash_distance: int = 14
    candidates: int = 32
    similarity: float = 0.92

    @classmethod
    def from_env(cls):
        values = {
            name: int(os.getenv(env, default)) for name, env, default in (
                ('download_bytes', 'IMAGE_DEDUP_DOWNLOAD_BYTES', 20 * 1024 * 1024),
                ('max_pixels', 'IMAGE_DEDUP_MAX_PIXELS', 20_000_000),
                ('phash_distance', 'IMAGE_DEDUP_PHASH_DISTANCE', 10),
                ('dhash_distance', 'IMAGE_DEDUP_DHASH_DISTANCE', 14),
                ('candidates', 'IMAGE_DEDUP_CANDIDATES', 32),
            )
        }
        values['similarity'] = float(os.getenv('IMAGE_DEDUP_SIMILARITY', '0.92'))
        result = cls(**values)
        if any(getattr(result, key) <= 0 for key in (
            'download_bytes', 'max_pixels', 'candidates'
        )) or not (0 <= result.phash_distance <= 64 and 0 <= result.dhash_distance <= 64
                  and 0 < result.similarity <= 1):
            raise ValueError('Invalid IMAGE_DEDUP settings')
        return result


class BoundedDownload(io.BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ImageCheckError('Картинка превышает лимит загрузки')
        return super().write(data)


RGB_SIZE = 128 * 128 * 3
HASH_PREFIX = 'image:v1:'


def init_schema(conn):
    """Idempotent migration; existing posts and non-images keep NULL hashes."""
    existing = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
    migration = os.path.join(os.path.dirname(__file__), 'migrations', '001_message_fingerprints.sql')
    with open(migration, encoding='utf-8') as source:
        for statement in source.read().split(';'):
            statement = statement.strip()
            if not statement:
                continue
            if statement.startswith('ALTER TABLE'):
                name = statement.split()[5]
                if name in existing:
                    continue
            conn.execute(statement)


@dataclass(frozen=True)
class Fingerprint:
    content_hash: str
    phash: int
    dhash: int
    rgb: bytes


def _bits(values):
    return int.from_bytes(np.packbits(values.reshape(-1)).tobytes(), 'big')


def prepare(data, settings):
    if len(data) > settings.download_bytes:
        raise ImageCheckError('Картинка превышает лимит загрузки')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > settings.max_pixels:
                    raise ImageCheckError('Картинка превышает лимит числа пикселей')
                if source.format not in ('JPEG', 'PNG', 'WEBP') or getattr(source, 'is_animated', False):
                    raise ImageCheckError('Проверка поддерживает только статичные JPEG, PNG и WebP')
                oriented = ImageOps.exif_transpose(source).convert('RGBA')
                background = Image.new('RGBA', oriented.size, 'white')
                background.alpha_composite(oriented)
                fitted = ImageOps.contain(background.convert('RGB'), (128, 128), Image.Resampling.BICUBIC)
                canvas = Image.new('RGB', (128, 128), 'white')
                canvas.paste(fitted, ((128-fitted.width)//2, (128-fitted.height)//2))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageCheckError('Не удалось безопасно прочитать картинку') from exc
    pixels = np.asarray(canvas, dtype=np.uint32)
    luma = ((299*pixels[:, :, 0] + 587*pixels[:, :, 1] + 114*pixels[:, :, 2] + 500)//1000).astype('uint8')
    gray = Image.fromarray(luma)
    small = np.asarray(gray.resize((32, 32), Image.Resampling.BICUBIC), dtype=float)
    basis = np.cos(np.pi * np.arange(8)[:, None] * (2*np.arange(32)+1) / 64)
    basis[0] /= np.sqrt(2)
    coefficients = basis @ small @ basis.T
    phash = _bits(coefficients > np.median(coefficients))
    differences = np.asarray(gray.resize((9, 8), Image.Resampling.BICUBIC))
    dhash = _bits(differences[:, :-1] > differences[:, 1:])
    rgb = canvas.tobytes()
    return Fingerprint(HASH_PREFIX + hashlib.sha256(rgb).hexdigest(), phash, dhash, rgb)


def _structural(a, b):
    # 256 blocks; preserve local differences such as changed meme text.
    a = a.reshape(16, 8, 16, 8, -1).transpose(0, 2, 1, 3, 4).reshape(256, 64, -1)
    b = b.reshape(16, 8, 16, 8, -1).transpose(0, 2, 1, 3, 4).reshape(256, 64, -1)
    ma, mb = a.mean(axis=1), b.mean(axis=1)
    da, db = a-ma[:, None, :], b-mb[:, None, :]
    va, vb = (da*da).sum(axis=1)/63, (db*db).sum(axis=1)/63
    covariance = (da*db).sum(axis=1)/63
    scores = ((2*ma*mb+6.5025)*(2*covariance+58.5225)
              / ((ma*ma+mb*mb+6.5025)*(va+vb+58.5225)))
    blocks = np.clip(scores, 0, 1).mean(axis=1)
    return float(0.75*blocks.mean() + 0.25*np.sort(blocks)[25])


def similarity(first, second):
    if len(first) != RGB_SIZE or len(second) != RGB_SIZE:
        raise ImageCheckError('Некорректный размер RGB-миниатюры в базе')
    a = np.frombuffer(first, dtype=np.uint8).reshape(128, 128, 3).astype(float)
    b = np.frombuffer(second, dtype=np.uint8).reshape(128, 128, 3).astype(float)
    return _structural(a, b)


class ImageHistory:
    def __init__(self, database, settings=None):
        self.database = database
        self.settings = settings or Settings.from_env()

    def find(self, *, unique_id=None, fingerprint=None):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            if unique_id:
                match = conn.execute('''SELECT * FROM messages WHERE file_unique_id=?
                    AND content_hash LIKE 'image:v1:%' AND image_rgb IS NOT NULL
                    ORDER BY posted DESC, message_id, chat_id LIMIT 1''', (unique_id,)).fetchone()
                if match is not None:
                    return dict(match)
            if fingerprint is None:
                return None
            match = conn.execute('''SELECT * FROM messages WHERE content_hash=?
                AND image_rgb IS NOT NULL ORDER BY posted DESC, message_id, chat_id LIMIT 1''',
                                 (fingerprint.content_hash,)).fetchone()
            if match is not None:
                return dict(match)

            def candidates():
                for row in conn.execute("""SELECT chat_id,message_id,image_phash,image_dhash
                    FROM messages WHERE image_phash IS NOT NULL AND image_dhash IS NOT NULL
                    AND content_hash LIKE 'image:v1:%'"""):
                    pd = (fingerprint.phash ^ int(row['image_phash'], 16)).bit_count()
                    dd = (fingerprint.dhash ^ int(row['image_dhash'], 16)).bit_count()
                    if pd <= self.settings.phash_distance and dd <= self.settings.dhash_distance:
                        yield pd, dd, row['message_id'], row['chat_id']

            best, best_score = None, self.settings.similarity
            # Keep only the best candidates in memory, regardless of history length.
            for _, _, message_id, chat_id in heapq.nsmallest(self.settings.candidates, candidates()):
                row = conn.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',
                                   (chat_id, message_id)).fetchone()
                if row['image_rgb'] is None:
                    continue
                score = similarity(fingerprint.rgb, row['image_rgb'])
                if score >= self.settings.similarity and (best is None or score > best_score):
                    best, best_score = dict(row), score
            return best


def store(conn, message, unique_id, fingerprint):
    if len(fingerprint.rgb) != RGB_SIZE:
        raise ImageCheckError('Некорректный размер RGB-миниатюры')
    # Queue insertion and fingerprint update share the caller's transaction.
    conn.execute('''UPDATE messages SET content_hash=?, file_unique_id=?, image_phash=?,
        image_dhash=?, image_rgb=? WHERE chat_id=? AND message_id=?''', (
            fingerprint.content_hash, unique_id, f'{fingerprint.phash:016x}',
            f'{fingerprint.dhash:016x}', fingerprint.rgb, message.chat.id, message.message_id,
        ))
