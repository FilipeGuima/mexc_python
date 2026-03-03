import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

env_path = BASE_DIR / ".env"
load_dotenv(env_path)

# --- Blofin Per-Strategy Credentials (no fallback — each bot must have its own) ---
BLOFIN_TP1_API_KEY = os.getenv("BLOFIN_TP1_API_KEY")
BLOFIN_TP1_SECRET_KEY = os.getenv("BLOFIN_TP1_SECRET_KEY")
BLOFIN_TP1_PASSPHRASE = os.getenv("BLOFIN_TP1_PASSPHRASE")

BLOFIN_TP3_API_KEY = os.getenv("BLOFIN_TP3_API_KEY")
BLOFIN_TP3_SECRET_KEY = os.getenv("BLOFIN_TP3_SECRET_KEY")
BLOFIN_TP3_PASSPHRASE = os.getenv("BLOFIN_TP3_PASSPHRASE")

BLOFIN_BREAKEVEN_API_KEY = os.getenv("BLOFIN_BREAKEVEN_API_KEY")
BLOFIN_BREAKEVEN_SECRET_KEY = os.getenv("BLOFIN_BREAKEVEN_SECRET_KEY")
BLOFIN_BREAKEVEN_PASSPHRASE = os.getenv("BLOFIN_BREAKEVEN_PASSPHRASE")

BLOFIN_SCALED_API_KEY = os.getenv("BLOFIN_SCALED_API_KEY")
BLOFIN_SCALED_SECRET_KEY = os.getenv("BLOFIN_SCALED_SECRET_KEY")
BLOFIN_SCALED_PASSPHRASE = os.getenv("BLOFIN_SCALED_PASSPHRASE")

BLOFIN_TP1_TRAIL_API_KEY = os.getenv("BLOFIN_TP1_TRAIL_API_KEY")
BLOFIN_TP1_TRAIL_SECRET_KEY = os.getenv("BLOFIN_TP1_TRAIL_SECRET_KEY")
BLOFIN_TP1_TRAIL_PASSPHRASE = os.getenv("BLOFIN_TP1_TRAIL_PASSPHRASE")

# --- Binance Per-Strategy Credentials (no fallback — each bot must have its own) ---
BINANCE_TP1_API_KEY = os.getenv("BINANCE_TP1_API_KEY")
BINANCE_TP1_SECRET_KEY = os.getenv("BINANCE_TP1_SECRET_KEY")

BINANCE_TP1_KILLERS_API_KEY = os.getenv("BINANCE_TP1_KILLERS_API_KEY", os.getenv("BINANCE_TP1_API_KEY"))
BINANCE_TP1_KILLERS_SECRET_KEY = os.getenv("BINANCE_TP1_KILLERS_SECRET_KEY", os.getenv("BINANCE_TP1_SECRET_KEY"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MEXC_TESTNET = os.getenv("MEXC_TESTNET", "true").lower() in ("true", "1", "yes")

# --- Blofin Testnet Settings (no fallback — must be explicitly set per strategy) ---
def _get_blofin_testnet(env_key: str) -> bool:
    val = os.getenv(env_key)
    if val is None:
        print(f" WARNING: {env_key} not set — defaulting to testnet=True for safety")
        return True
    return val.lower() in ("true", "1", "yes")

BLOFIN_TP1_TESTNET = _get_blofin_testnet("BLOFIN_TP1_TESTNET")
BLOFIN_TP3_TESTNET = _get_blofin_testnet("BLOFIN_TP3_TESTNET")
BLOFIN_BREAKEVEN_TESTNET = _get_blofin_testnet("BLOFIN_BREAKEVEN_TESTNET")
BLOFIN_SCALED_TESTNET = _get_blofin_testnet("BLOFIN_SCALED_TESTNET")
BLOFIN_TP1_TRAIL_TESTNET = _get_blofin_testnet("BLOFIN_TP1_TRAIL_TESTNET")

# --- Binance Testnet Settings (no fallback — must be explicitly set) ---
_binance_tp1_testnet_raw = os.getenv("BINANCE_TP1_TESTNET")
if _binance_tp1_testnet_raw is None:
    print(" WARNING: BINANCE_TP1_TESTNET not set — defaulting to testnet=True for safety")
    BINANCE_TP1_TESTNET = True
else:
    BINANCE_TP1_TESTNET = _binance_tp1_testnet_raw.lower() in ("true", "1", "yes")

_binance_tp1_killers_testnet_raw = os.getenv("BINANCE_TP1_KILLERS_TESTNET")
if _binance_tp1_killers_testnet_raw is None:
    print(" WARNING: BINANCE_TP1_KILLERS_TESTNET not set — defaulting to testnet=True for safety")
    BINANCE_TP1_KILLERS_TESTNET = True
else:
    BINANCE_TP1_KILLERS_TESTNET = _binance_tp1_killers_testnet_raw.lower() in ("true", "1", "yes")

# --- MEXC Per-Strategy Tokens (no fallback — each bot must have its own) ---
TP1_TOKEN = os.getenv("TP1_MEXC_TOKEN")
TP3_TOKEN = os.getenv("TP3_MEXC_TOKEN")
BREAKEVEN_TOKEN = os.getenv("BREAKEVEN_MEXC_TOKEN")
USER_LISTENER_TOKEN = os.getenv("USER_LISTENER_MEXC_TOKEN")

chats_str = os.getenv("TARGET_CHATS", "")
TARGET_CHATS = [int(x.strip()) for x in chats_str.split(',') if x.strip()]

def _get_target_chats(env_key: str) -> list:
    """Per-strategy chat override. Falls back to TARGET_CHATS if not set."""
    val = os.getenv(env_key, "")
    if val.strip():
        return [int(x.strip()) for x in val.split(',') if x.strip()]
    return TARGET_CHATS

# --- Per-Strategy Target Chats (defaults to TARGET_CHATS if not set) ---
# MEXC
MEXC_TP1_CHATS = _get_target_chats("MEXC_TP1_TARGET_CHATS")
MEXC_TP3_CHATS = _get_target_chats("MEXC_TP3_TARGET_CHATS")
MEXC_BREAKEVEN_CHATS = _get_target_chats("MEXC_BREAKEVEN_TARGET_CHATS")
# Blofin
BLOFIN_BREAKEVEN_CHATS = _get_target_chats("BLOFIN_BREAKEVEN_TARGET_CHATS")
BLOFIN_TP3_CHATS = _get_target_chats("BLOFIN_TP3_TARGET_CHATS")
BLOFIN_SCALED_CHATS = _get_target_chats("BLOFIN_SCALED_TARGET_CHATS")
BLOFIN_TP1_TRAIL_CHATS = _get_target_chats("BLOFIN_TP1_TRAIL_TARGET_CHATS")
# Binance
BINANCE_TP1_CHATS = _get_target_chats("BINANCE_TP1_TARGET_CHATS")
BINANCE_TP1_KILLERS_CHATS = _get_target_chats("BINANCE_TP1_KILLERS_TARGET_CHATS")

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
SESSION_BLOFIN_TP1_TRAIL = SESSION_DIR / "blofin_tp1_trail_session"

# --- Binance Sessions ---
SESSION_BINANCE_TP1 = SESSION_DIR / "binance_tp1_session"
SESSION_BINANCE_TP1_KILLERS = SESSION_DIR / "binance_tp1_killers_session"

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
        elif api_key:
            # API key exists but EXCHANGE not set — refuse to guess
            print(f" ERROR: STATS_BOT{stats_bot_index} has API_KEY but no EXCHANGE set. "
                  f"Set {exchange_key}=blofin or {exchange_key}=binance")
            stats_bot_index += 1
            continue
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
        if per_account_testnet is None:
            print(f" ERROR: STATS_BOT{stats_bot_index}_TESTNET not set for Blofin account. "
                  f"Set it to 'true' or 'false' explicitly.")
            stats_bot_index += 1
            continue
        testnet = per_account_testnet.lower() in ("true", "1", "yes")
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
    elif exchange == "binance":
        secret_key = os.getenv(f"STATS_BOT{stats_bot_index}_SECRET_KEY")
        per_account_testnet = os.getenv(f"STATS_BOT{stats_bot_index}_TESTNET")
        if per_account_testnet is None:
            print(f" ERROR: STATS_BOT{stats_bot_index}_TESTNET not set for Binance account. "
                  f"Set it to 'true' or 'false' explicitly.")
            stats_bot_index += 1
            continue
        testnet = per_account_testnet.lower() in ("true", "1", "yes")
        if not all([api_key, secret_key]):
            print(f" ERROR: Incomplete Binance credentials for STATS_BOT{stats_bot_index} — "
                  f"need API_KEY and SECRET_KEY")
            stats_bot_index += 1
            continue
        STATS_ACCOUNTS.append({
            "account_id": f"BOT{stats_bot_index}",
            "exchange": "binance",
            "api_key": api_key,
            "secret_key": secret_key,
            "testnet": testnet,
        })
    else:
        print(f" WARNING: Unknown exchange '{exchange}' for STATS_BOT{stats_bot_index}")

    stats_bot_index += 1


if not API_ID or not API_HASH:
    print(f" WARNING: API_ID/HASH missing in {env_path}")