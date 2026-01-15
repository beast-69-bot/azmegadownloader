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
- `/leech <mega link>`: download and upload to current chat
- `/start`, `/help`, `/ping`
