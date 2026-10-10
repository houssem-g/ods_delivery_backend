"""getSupportContacts — the support phone / WhatsApp numbers set in AppSettings (key 'main'),
for everybody, signed in or not (legal pages are shown before login).

Answers only the two numbers, re-validated: an invalid stored value is answered as null.
Same rules as src/lib/supportContacts.js.
"""

import re
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.security.deps import CurrentUser

AUTH = "optional"
_ALLOWED = re.compile(r"^[+\d\s().\-/]+$")


def normalize_support_phone(raw: Any) -> str | None:
    """'+216XXXXXXXX' for a Tunisian number (8 digits, bare or with 216 / +216 / 00216),
    '+<8-15 digits>' for an international one written with + or 00; None otherwise."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > 32 or not _ALLOWED.match(text):
        return None
    if text.find("+") > 0 or text.count("+") > 1:
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 8:
        digits = f"216{digits}"
    elif not (len(digits) == 11 and digits.startswith("216")):
        return None
    if digits.startswith("216"):
        return f"+{digits}" if re.fullmatch(r"216[2-9]\d{7}", digits) else None
    return f"+{digits}" if re.fullmatch(r"[1-9]\d{7,14}", digits) else None


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    row = await session.get(AppSetting, "main")
    value = row.value if row is not None else {}
    return 200, {
        "success": True,
        "support_phone": normalize_support_phone(value.get("support_phone")),
        "support_whatsapp": normalize_support_phone(value.get("support_whatsapp")),
    }
