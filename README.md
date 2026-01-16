# Mega Leech Bot (MEGA only)

Minimal Telegram bot that downloads MEGA links (file/folder) and uploads to Telegram.

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
- `/settings`: open leech settings panel (per-user)
- `/start`, `/help`, `/ping`, `/cancel <task_id>`

## Settings panel
- Per-user settings stored in `settings.db`
- `THUMBNAIL`: upload a custom thumbnail (and toggle THUMB)
- `SPLIT SIZE`: split large files before upload
- `DESTINATION`: subfolder under `downloads/` for temporary storage
- `PREFIX`/`SUFFIX`: rename files before upload
- `CAPTION`: template with `{filename}`, `{basename}`, `{ext}`
- `LAYOUT`: appended to caption
