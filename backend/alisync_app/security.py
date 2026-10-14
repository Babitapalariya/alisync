import base64
import hashlib
import hmac
import json
import os
import time
from cryptography.fernet import Fernet, InvalidToken

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User

bearer = HTTPBearer(auto_error=False)


def _secret_cipher() -> Fernet:
    secret = get_settings().APP_SECRET
    if len(secret) < 32:
        raise HTTPException(503, "APP_SECRET must contain at least 32 characters")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _secret_cipher().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return _secret_cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(500, "Stored AliExpress credential cannot be decrypted") from exc


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, expected = encoded.split("$", 3)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_token(user_id: int, expires_seconds: int = 86400) -> str:
    secret = get_settings().APP_SECRET
    if len(secret) < 32:
        raise HTTPException(503, "APP_SECRET must contain at least 32 characters")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": user_id, "exp": int(time.time()) + expires_seconds}).encode()).rstrip(b"=")
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return payload.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def decode_token(token: str) -> int:
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = payload_text.encode()
        supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(get_settings().APP_SECRET.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4)))
        if int(data["exp"]) < int(time.time()):
            raise ValueError
        return int(data["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid or expired AliSync session")


def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not credentials:
        raise HTTPException(401, "Authentication required")
    user = db.query(User).filter(User.id == decode_token(credentials.credentials), User.is_active == True).first()
    if not user:
        raise HTTPException(401, "User account is unavailable")
    db.info["user_id"] = user.id
    return user
