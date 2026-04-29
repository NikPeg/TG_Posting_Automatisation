import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

START_HOUR = int(os.getenv('START_HOUR', 0))
START_MINUTE = int(os.getenv('START_MINUTE', 0))
END_HOUR = int(os.getenv('END_HOUR', 23))
END_MINUTE = int(os.getenv('END_MINUTE', 59))
POSTING_INTERVAL = float(os.getenv('POSTING_INTERVAL', 60))
LAST_TIME_POST = datetime.fromisoformat(os.getenv('LAST_TIME_POST'))
LAST_RESET_DATE = datetime.fromisoformat(os.getenv('LAST_RESET_DATE'))
RESET_INTERVAL_DAYS = int(os.getenv('RESET_INTERVAL_DAYS', 7))
TIMEZONE_OFFSET = int(os.getenv('TIMEZONE_OFFSET', 0))
