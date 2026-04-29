"""
Хранит runtime-состояние бота (не конфиг администратора) в runtime_data.json.
"""
import json
import os
from datetime import datetime, timezone

RUNTIME_FILE = "runtime_data.json"
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _load() -> dict:
    if os.path.exists(RUNTIME_FILE):
        try:
            with open(RUNTIME_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict):
    with open(RUNTIME_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_last_post_time() -> datetime:
    data = _load()
    raw = data.get("last_time_post")
    if raw:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    # Фоллбэк: читаем из .env для обратной совместимости
    from dotenv import load_dotenv
    load_dotenv(override=True)
    env_val = os.getenv("LAST_TIME_POST")
    if env_val:
        dt = datetime.fromisoformat(env_val)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return _EPOCH


def set_last_post_time(dt: datetime):
    data = _load()
    data["last_time_post"] = dt.isoformat()
    _save(data)
