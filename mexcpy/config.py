import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

env_path = BASE_DIR / ".env"
load_dotenv(env_path)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MEXC_TOKEN = os.getenv("MEXC_TOKEN")

TP1_TOKEN = os.getenv("TP1_MEXC_TOKEN", MEXC_TOKEN)
BREAKEVEN_TOKEN = os.getenv("BREAKEVEN_MEXC_TOKEN", MEXC_TOKEN)
USER_LISTENER_TOKEN = os.getenv("USER_LISTENER_MEXC_TOKEN", MEXC_TOKEN)

chats_str = os.getenv("TARGET_CHATS", "")
TARGET_CHATS = [int(x.strip()) for x in chats_str.split(',') if x.strip()]

SESSION_DIR = BASE_DIR / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

SESSION_TP1 = SESSION_DIR / "tp1_session"
SESSION_BREAKEVEN = SESSION_DIR / "breakeven_session"
SESSION_USER = SESSION_DIR / "user_listener_session"

SESSION_MAIN = SESSION_DIR / "anon_session"

# --- Logic for Stats Bots ---
STATS_ACCOUNTS = []
stats_bot_index = 1
while True:
    token_key = f"STATS_BOT{stats_bot_index}_TOKEN"

    token = os.getenv(token_key)

    if not token:
        break

    STATS_ACCOUNTS.append({
        "account_id": f"BOT{stats_bot_index}",
        "token": token,
    })
    stats_bot_index += 1


if not API_ID or not API_HASH:
    print(f" WARNING: API_ID/HASH missing in {env_path}")