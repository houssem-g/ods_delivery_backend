"""Secrets kept at rest encrypted: Fernet (AES-128-CBC + HMAC-SHA256), key PARTNER_SECRET_KEY.

Used for `partners.webhook_secret`: the server needs the clear secret to sign the webhooks, so it
is encrypted (not hashed). Stored as `fernet:<token>`; a value without the prefix is refused rather
than used as clear text. PARTNER_SECRET_KEY may hold `new,old` while the key is rotated: the first
key encrypts, every key decrypts (MultiFernet).
Model: ods-be app/services/social/crypto.py.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import partner_secret_keys, settings

PREFIX = "fernet:"


class SecretKeyError(RuntimeError):
    """No usable PARTNER_SECRET_KEY, or a value it cannot decrypt (key changed?)."""


def _box(raw_keys: str | None = None) -> MultiFernet:
    keys = partner_secret_keys(settings.PARTNER_SECRET_KEY if raw_keys is None else raw_keys)
    if not keys:
        raise SecretKeyError("PARTNER_SECRET_KEY is not configured")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as exc:
        raise SecretKeyError("PARTNER_SECRET_KEY is not a valid Fernet key") from exc


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(value: str | None, raw_keys: str | None = None) -> str | None:
    if not value:
        return None
    return PREFIX + _box(raw_keys).encrypt(value.encode()).decode()


def decrypt(value: str | None, raw_keys: str | None = None) -> str | None:
    if not value:
        return None
    if not value.startswith(PREFIX):
        raise SecretKeyError("stored secret is not encrypted (migration partner_privacy not applied?)")
    try:
        return _box(raw_keys).decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretKeyError("stored secret cannot be decrypted (PARTNER_SECRET_KEY changed?)") from exc
