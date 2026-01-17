# Mega Leech Bot (MEGA only)

Minimal Telegram bot that downloads MEGA links (file/folder) and uploads to Telegram using pure `mega.py` (no SDK).

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
- Uses `mega.py` for both file and folder links (recursive folder download).

## Commands
User:
- `/start`: start the bot
- `/help`: show help menu
- `/ping`: bot status
- `/status`: plan + usage summary
- `/leech <mega link>`: download and upload to current chat
- `/cancel <task_id>`: cancel your own task
- `/settings`: chat ID/caption/thumbnail settings
- `/speedtest`: check server speed
- `/pay`: premium payment flow
- `/redeem <token>`: redeem 1-day premium token

Admin/Sudo:
- `/setlogchannel <channel_id or @username>`: set /start log channel
- `/settaskchannel <channel_id or @username>`: set task log channel
- `/addadmin <user_id or @username>`: add admin
- `/deladmin <user_id or @username>`: remove admin
- `/listadmins`: list admins
- `/setpremium <user_id or @username> <validity>`: enable premium (1w/1m/1y)
- `/delpremium <user_id or @username>`: disable premium
- `/listpremium`: list premium users
- `/ban <user_id or @username>`: ban user
- `/unban <user_id or @username>`: unban user
- `/listbans`: list banned users
- `/generate <qty>`: generate premium tokens (1 hour expiry, single-use)
- `/bsetting`: verification/support/payment settings panel
