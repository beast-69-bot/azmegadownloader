# Mega Leech Bot (MEGA only)

Minimal Telegram bot that downloads MEGA links (file/folder with SDK) and uploads to Telegram.

## Setup
1) Install deps:
```
pip install -r requirements.txt
```

2) Configure `config.py` (use `config_sample.py` as template).

3) Run:
```
python -m bot
```

## MEGA support
- Uses `mega.py` for both file and folder links.

## Commands
User:
- `/leech <mega link>`: download and upload to current chat
- `/cancel <task_id>`: cancel your own task
- `/settings`: chat ID/caption/thumbnail settings
- `/start`, `/help`, `/ping`

Admin/Sudo:
- `/setlogchannel <channel_id or @username>`: set /start log channel
- `/settaskchannel <channel_id or @username>`: set task log channel
- `/addadmin <user_id or @username>`: add admin
- `/deladmin <user_id or @username>`: remove admin
- `/listadmins`: list admins
- `/bsetting`: verification settings panel
