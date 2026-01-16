import hmac
import hashlib
import base64
import time
import json
import uuid


def get_auth_headers(request_path, method, body, api_key, secret_key, passphrase):
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())

    body_str = ""
    if body:
        body_str = json.dumps(body, separators=(',', ':'))
    if method == "GET":
        body_str = ""

    prehash = f"{request_path}{method}{timestamp}{nonce}{body_str}"

    signature_hex = hmac.new(
        secret_key.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    sign_b64 = base64.b64encode(signature_hex.encode("utf-8")).decode("utf-8")

    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json"
    }