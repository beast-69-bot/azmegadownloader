import asyncio
import logging
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT_DIR / "mega_leech.log"

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()],
)

LOGGER = logging.getLogger("mega_leech")

try:
    BOT_LOOP = asyncio.get_event_loop()
except RuntimeError:
    BOT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(BOT_LOOP)
