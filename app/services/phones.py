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
    if not digits or (text.startswith("+") and len(digits) <= 3):
        return None
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhone(raw) from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhone(raw)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
