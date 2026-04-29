from datetime import datetime, timezone, timedelta
import config

def tz_now():
    return datetime.now(timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
