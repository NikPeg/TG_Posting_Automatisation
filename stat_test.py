import os
import asyncio
import logging
from dotenv import load_dotenv
from telethon import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

async def collect_stats():
    load_dotenv(override=True)

    api_id = os.getenv("CORE_API_ID")
    api_hash = os.getenv("CORE_API_HASH")
    channel_id = int(os.getenv("CHANNEL_ID"))

    if not api_id or not api_hash:
        logger.error("CORE_API_ID / CORE_API_HASH not found")
        return

    async with TelegramClient("stats_session", int(api_id), api_hash) as client:
        logger.info("Client connected")

        test_message_ids = [
            28667, 28669, 28661, 28668, 28675
        ]

        for msg_id in test_message_ids:
            try:
                message = await client.get_messages(channel_id, ids=msg_id)

                if not message:
                    logger.warning(f"Message {msg_id} not found")
                    continue

                views = message.views or 0

                if message.reactions:
                    
                    reactions = sum(r.count for r in message.reactions.results)

                logger.info(
                    f"msg {msg_id}: views={views}, reactions={reactions}"
                )

            except Exception as e:
                logger.error(f"Error for msg {msg_id}: {e}")

if __name__ == "__main__":
    asyncio.run(collect_stats())
