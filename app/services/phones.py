"""Phone numbers to E.164 (Tunisia by default, like the Deno functions)."""

import phonenumbers

DEFAULT_REGION = "TN"


class InvalidPhone(ValueError):
    pass


def to_e164(raw: str | None, region: str = DEFAULT_REGION) -> str | None:
    """'+216 22 123 456', '22123456', '0021622123456' -> '+21622123456'; blank or a bare
    country code -> None; anything else that is not a valid number -> InvalidPhone."""
    if raw is None:
        return None
    text = raw.strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not text or (text.startswith("+") and 0 < len(digits) <= 3 and text[1:].strip().isdigit()):
        return None
    if not digits:
        raise InvalidPhone(raw)
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhone(raw) from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhone(raw)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


TUNISIA_PREFIX = "+216"


def is_tunisian(e164: str | None) -> bool:
    """A stored E.164 number of Tunisia (accepted without verification)."""
    return bool(e164) and str(e164).startswith(TUNISIA_PREFIX)


def is_international_mobile(e164: str | None) -> bool:
    """A valid non-Tunisian E.164 number that may be a mobile (can receive WhatsApp). Used
    for the verification code and the messages to a verified foreign number."""
    if not e164 or is_tunisian(e164) or not str(e164).startswith("+"):
        return False
    try:
        parsed = phonenumbers.parse(str(e164), None)
    except phonenumbers.NumberParseException:
        return False
    if not phonenumbers.is_valid_number(parsed):
        return False
    kind = phonenumbers.number_type(parsed)
    return kind in (
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    )


def customer_phone_verified(phone_e164: str | None, phone_verified_at: object | None) -> bool:
    """Tunisian numbers always count as verified; a foreign one once confirmed by code
    (the trigger `trg_users_phone_unverify` clears the date when the number changes)."""
    if not phone_e164:
        return False
    return is_tunisian(phone_e164) or phone_verified_at is not None


def verification_enforced() -> bool:
    """Foreign numbers need the WhatsApp code only once WhatsApp is configured: until the
    WhatsApp Business account exists they are accepted as they are (owner, 2026-09-29)."""
    from app.config import settings

    return settings.whatsapp_enabled


def customer_phone_usable(phone_e164: str | None, phone_verified_at: object | None) -> bool:
    """May a customer order / reserve with this stored number? Tunisian or verified always;
    an unverified foreign one only while verification can't be enforced."""
    if not phone_e164:
        return False
    return customer_phone_verified(phone_e164, phone_verified_at) or not verification_enforced()
