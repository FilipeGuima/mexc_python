import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

env_path = BASE_DIR / ".env"
load_dotenv(env_path)

# --- Blofin Base Credentials (fallback) ---
BLOFIN_API_KEY = os.getenv("BLOFIN_API_KEY")
BLOFIN_SECRET_KEY = os.getenv("BLOFIN_SECRET_KEY")
BLOFIN_PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE")

# --- Blofin Per-Strategy Credentials ---
# Each strategy can have its own account, falling back to base credentials
BLOFIN_TP1_API_KEY = os.getenv("BLOFIN_TP1_API_KEY", BLOFIN_API_KEY)
BLOFIN_TP1_SECRET_KEY = os.getenv("BLOFIN_TP1_SECRET_KEY", BLOFIN_SECRET_KEY)
BLOFIN_TP1_PASSPHRASE = os.getenv("BLOFIN_TP1_PASSPHRASE", BLOFIN_PASSPHRASE)

BLOFIN_TP3_API_KEY = os.getenv("BLOFIN_TP3_API_KEY", BLOFIN_API_KEY)
BLOFIN_TP3_SECRET_KEY = os.getenv("BLOFIN_TP3_SECRET_KEY", BLOFIN_SECRET_KEY)
BLOFIN_TP3_PASSPHRASE = os.getenv("BLOFIN_TP3_PASSPHRASE", BLOFIN_PASSPHRASE)

BLOFIN_BREAKEVEN_API_KEY = os.getenv("BLOFIN_BREAKEVEN_API_KEY", BLOFIN_API_KEY)
BLOFIN_BREAKEVEN_SECRET_KEY = os.getenv("BLOFIN_BREAKEVEN_SECRET_KEY", BLOFIN_SECRET_KEY)
BLOFIN_BREAKEVEN_PASSPHRASE = os.getenv("BLOFIN_BREAKEVEN_PASSPHRASE", BLOFIN_PASSPHRASE)

BLOFIN_SCALED_API_KEY = os.getenv("BLOFIN_SCALED_API_KEY", BLOFIN_API_KEY)
BLOFIN_SCALED_SECRET_KEY = os.getenv("BLOFIN_SCALED_SECRET_KEY", BLOFIN_SECRET_KEY)
BLOFIN_SCALED_PASSPHRASE = os.getenv("BLOFIN_SCALED_PASSPHRASE", BLOFIN_PASSPHRASE)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MEXC_TOKEN = os.getenv("MEXC_TOKEN")
MEXC_TESTNET = os.getenv("MEXC_TESTNET", "true").lower() in ("true", "1", "yes")

# --- Blofin Testnet Settings ---
BLOFIN_TESTNET = os.getenv("BLOFIN_TESTNET", "true").lower() in ("true", "1", "yes")

# Per-strategy testnet (fallback to global BLOFIN_TESTNET)
def _get_blofin_testnet(env_key: str) -> bool:
    val = os.getenv(env_key)
    if val is not None:
        return val.lower() in ("true", "1", "yes")
    return BLOFIN_TESTNET

BLOFIN_TP1_TESTNET = _get_blofin_testnet("BLOFIN_TP1_TESTNET")
BLOFIN_TP3_TESTNET = _get_blofin_testnet("BLOFIN_TP3_TESTNET")
BLOFIN_BREAKEVEN_TESTNET = _get_blofin_testnet("BLOFIN_BREAKEVEN_TESTNET")
BLOFIN_SCALED_TESTNET = _get_blofin_testnet("BLOFIN_SCALED_TESTNET")

TP1_TOKEN = os.getenv("TP1_MEXC_TOKEN", MEXC_TOKEN)
TP3_TOKEN = os.getenv("TP3_MEXC_TOKEN", MEXC_TOKEN)
BREAKEVEN_TOKEN = os.getenv("BREAKEVEN_MEXC_TOKEN", MEXC_TOKEN)
USER_LISTENER_TOKEN = os.getenv("USER_LISTENER_MEXC_TOKEN", MEXC_TOKEN)

chats_str = os.getenv("TARGET_CHATS", "")
TARGET_CHATS = [int(x.strip()) for x in chats_str.split(',') if x.strip()]

SESSION_DIR = BASE_DIR / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# --- MEXC Sessions ---
SESSION_TP1 = SESSION_DIR / "tp1_session"
SESSION_BREAKEVEN = SESSION_DIR / "breakeven_session"
SESSION_TP3 = SESSION_DIR / "tp3_session"
SESSION_SCALED = SESSION_DIR / "scaled_session"
SESSION_USER = SESSION_DIR / "user_listener_session"
SESSION_MAIN = SESSION_DIR / "anon_session"

# --- Blofin Sessions (separate from MEXC to allow parallel operation) ---
SESSION_BLOFIN_TP1 = SESSION_DIR / "blofin_tp1_session"
SESSION_BLOFIN_TP3 = SESSION_DIR / "blofin_tp3_session"
SESSION_BLOFIN_BREAKEVEN = SESSION_DIR / "blofin_breakeven_session"
SESSION_BLOFIN_SCALED = SESSION_DIR / "blofin_scaled_session"

# --- Logic for Stats Bots ---
STATS_ACCOUNTS = []
stats_bot_index = 1
while True:
    exchange_key = f"STATS_BOT{stats_bot_index}_EXCHANGE"
    token_key = f"STATS_BOT{stats_bot_index}_TOKEN"
    api_key_key = f"STATS_BOT{stats_bot_index}_API_KEY"

    exchange = os.getenv(exchange_key, "").lower()
    token = os.getenv(token_key)
    api_key = os.getenv(api_key_key)

    if not exchange:
        # Backward compat: if TOKEN exists but no EXCHANGE, assume mexc
        if token:
            exchange = "mexc"
        else:
            # No token and no exchange — check if blofin creds exist
            if api_key:
                exchange = "blofin"
            else:
                break

    if exchange == "mexc":
        if not token:
            break
        STATS_ACCOUNTS.append({
            "account_id": f"BOT{stats_bot_index}",
            "exchange": "mexc",
            "token": token,
            "testnet": MEXC_TESTNET,
        })
    elif exchange == "blofin":
        secret_key = os.getenv(f"STATS_BOT{stats_bot_index}_SECRET_KEY")
        passphrase = os.getenv(f"STATS_BOT{stats_bot_index}_PASSPHRASE")
        per_account_testnet = os.getenv(f"STATS_BOT{stats_bot_index}_TESTNET")
        if per_account_testnet is not None:
            testnet = per_account_testnet.lower() in ("true", "1", "yes")
        else:
            testnet = BLOFIN_TESTNET
        if not all([api_key, secret_key, passphrase]):
            print(f" WARNING: Incomplete Blofin credentials for STATS_BOT{stats_bot_index}")
            stats_bot_index += 1
            continue
        STATS_ACCOUNTS.append({
            "account_id": f"BOT{stats_bot_index}",
            "exchange": "blofin",
            "api_key": api_key,
            "secret_key": secret_key,
            "passphrase": passphrase,
            "testnet": testnet,
        })
    else:
        print(f" WARNING: Unknown exchange '{exchange}' for STATS_BOT{stats_bot_index}")

    stats_bot_index += 1


if not API_ID or not API_HASH:
    print(f" WARNING: API_ID/HASH missing in {env_path}")