import asyncio
import logging

from app.bot import setup_bot
from app.db.pool import create_pool, close_pool
from app.db.schema import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
# httpx logs every request URL at INFO, and a Last.fm URL carries the API key
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("maspotifybot")


async def main():
    log.info("Starting MaSpotifyBot...")

    pool = await create_pool()
    await apply_schema(pool)
    log.info("Database ready")

    try:
        await setup_bot(pool)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
