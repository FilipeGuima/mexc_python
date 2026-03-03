import hmac
import hashlib
import time


def get_timestamp() -> int:
    """Return current timestamp in milliseconds."""
    return int(time.time() * 1000)


def get_signature(query_string: str, secret_key: str) -> str:
    """HMAC-SHA256 signature of the query string, returned as hex digest."""
    return hmac.new(
        secret_key.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def get_auth_headers(api_key: str) -> dict:
    """Return Binance auth headers (API key only, signature goes in query string)."""
    return {
        "X-MBX-APIKEY": api_key,
        "Content-Type": "application/x-www-form-urlencoded"
    }
