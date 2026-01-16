from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
USER_CONFIG_PATH = ROOT_DIR / "config.py"


def _load_user_config():
    if not USER_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config.py at {USER_CONFIG_PATH}")
    spec = spec_from_file_location("user_config", USER_CONFIG_PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_user_cfg = _load_user_config()


def _get(name: str, default=None):
    return getattr(_user_cfg, name, default)


BOT_TOKEN = _get("BOT_TOKEN")
OWNER_ID = int(_get("OWNER_ID", 0) or 0)
TELEGRAM_API = int(_get("TELEGRAM_API", 0) or 0)
TELEGRAM_HASH = _get("TELEGRAM_HASH", "")

MEGA_EMAIL = _get("MEGA_EMAIL", "")
MEGA_PASSWORD = _get("MEGA_PASSWORD", "")

DOWNLOAD_DIR = Path(_get("DOWNLOAD_DIR", ROOT_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATUS_UPDATE_INTERVAL = int(_get("STATUS_UPDATE_INTERVAL", 5) or 5)
CONCURRENT_DOWNLOADS = int(_get("CONCURRENT_DOWNLOADS", 2) or 2)
CONCURRENT_UPLOADS = int(_get("CONCURRENT_UPLOADS", 2) or 2)

AUTHORIZED_CHATS = _get("AUTHORIZED_CHATS", "")
SUDO_USERS = _get("SUDO_USERS", "")
VERIFY_EXPIRE = int(_get("VERIFY_EXPIRE", 0) or 0)
TOKEN_TTL = int(_get("TOKEN_TTL", 0) or 0)
MIN_TOKEN_AGE = int(_get("MIN_TOKEN_AGE", 0) or 0)
VERIFY_PHOTO = _get("VERIFY_PHOTO", "")
VERIFY_TUTORIAL = _get("VERIFY_TUTORIAL", "")
SHORTLINK_SITE = _get("SHORTLINK_SITE", "")
SHORTLINK_API = _get("SHORTLINK_API", "")


def parse_id_list(value: str) -> set[int]:
    ids = set()
    for item in (value or "").replace(" ", "").split(","):
        if not item:
            continue
        if item.lstrip("-").isdigit():
            ids.add(int(item))
    return ids


AUTHORIZED_CHAT_IDS = parse_id_list(AUTHORIZED_CHATS)
SUDO_USER_IDS = parse_id_list(SUDO_USERS)
