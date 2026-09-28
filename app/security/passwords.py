"""bcrypt password hashing (passlib)."""

from passlib.context import CryptContext

_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)
# bcrypt only reads 72 bytes; longer inputs are refused rather than silently truncated.
MAX_PASSWORD_BYTES = 72
_DUMMY_HASH = _context.hash("timing-equaliser")


def hash_password(password: str) -> str:
    return _context.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: a missing hash still costs one bcrypt round."""
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return False
    if not password_hash:
        _context.verify(password, _DUMMY_HASH)
        return False
    return _context.verify(password, password_hash)


def password_problem(password: str, min_length: int) -> str | None:
    """Why a new password is refused, or None."""
    if len(password) < min_length:
        return f"Password is too short (at least {min_length} characters)"
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return f"Password is too long (at most {MAX_PASSWORD_BYTES} bytes)"
    return None
