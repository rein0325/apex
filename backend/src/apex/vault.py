"""網站上的私人內容：用密碼加密後才放上公開網站，只有知道密碼的人能在瀏覽器解開。

格式與瀏覽器 WebCrypto 相容：PBKDF2-SHA256 由密碼產生 256 位元金鑰，AES-GCM 加密
（密文尾端附 16 位元組驗證碼）。
密碼只從環境變數 APEX_VIEW_PASSWORD 讀取（GitHub Secrets），不寫進程式或紀錄。
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PASSWORD_ENV = "APEX_VIEW_PASSWORD"
ITERATIONS = 310_000


def _key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(_key(password, salt)).encrypt(iv, data, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITERATIONS,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(box: dict, password: str):
    d = lambda k: base64.b64decode(box[k])  # noqa: E731
    pt = AESGCM(_key(password, d("salt"), box["iter"])).decrypt(d("iv"), d("ct"), None)
    return json.loads(pt.decode("utf-8"))


def password() -> str | None:
    return os.environ.get(PASSWORD_ENV) or None
