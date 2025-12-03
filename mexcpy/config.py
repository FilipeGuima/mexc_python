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

chats_str = os.getenv("TARGET_CHATS", "")
TARGET_CHATS = [int(x.strip()) for x in chats_str.split(',') if x.strip()]

SESSION_DIR = BASE_DIR / "sessions"
SESSION_FILE = SESSION_DIR / "anon_session"

SESSION_DIR.mkdir(parents=True, exist_ok=True)

if not API_ID or not API_HASH:
    print(f" WARNING: API_ID/HASH missing in {env_path}")