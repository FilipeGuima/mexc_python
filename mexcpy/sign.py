import hmac
import hashlib

def generate_signature(api_key: str, secret_key: str, timestamp: str, data: str = "") -> str:

    message = timestamp + api_key + data
    signature = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature