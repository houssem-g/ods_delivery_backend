"""Object key conventions and upload rules.

public/<purpose>/<yyyy>/<mm>/<uuid>.<ext>            anonymously readable (shop, hot-deal photos)
private/<purpose>/<owner uuid>/<uuid>.<ext>           presigned GET only
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Literal

Visibility = Literal["public", "private"]

# Declared type -> (extension, magic-bytes check)
IMAGE_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
}
PRIVATE_EXTRA_TYPES: dict[str, str] = {"application/pdf": "pdf"}
# Voice notes of the order chat (private, purpose "chat" only): what MediaRecorder produces on
# Android / desktop (webm, ogg) and iOS (mp4 / m4a / aac), plus mp3 and wav.
AUDIO_TYPES: dict[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}
CHAT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
CHAT_AUDIO_MAX_BYTES = 3 * 1024 * 1024

# Purposes the front may name. Courier ID documents (and the documents of courierDocuments) are
# signed only by the admin functions.
PURPOSES = {
    "generic", "receipt", "issue", "hot_deal", "shop", "review", "menu", "courier_id", "chat", "courier_doc",
    "credit_receipt",
}  # fmt: skip
# credit_receipt: the bank-counter receipt of a prepaid-credit top-up (app/services/credit.py).
ADMIN_SIGNED_PURPOSES = {"courier_id", "courier_doc", "credit_receipt"}
_PURPOSE_RE = re.compile(r"^[a-z_]{1,32}$")


def allowed_types(visibility: Visibility, purpose: str | None = None) -> dict[str, str]:
    if visibility != "private":
        return IMAGE_TYPES
    return IMAGE_TYPES | PRIVATE_EXTRA_TYPES | (AUDIO_TYPES if purpose == "chat" else {})


def max_bytes(content_type: str, purpose: str, default: int) -> int:
    """Chat attachments are smaller than the general upload limit."""
    if purpose != "chat":
        return default
    return min(default, CHAT_AUDIO_MAX_BYTES if content_type in AUDIO_TYPES else CHAT_IMAGE_MAX_BYTES)


def sniff_matches(content_type: str, head: bytes) -> bool:
    """The declared type must match the file's first bytes (no HTML/SVG served from our bucket)."""
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type in ("image/heic", "image/heif"):
        return head[4:8] == b"ftyp"
    if content_type == "application/pdf":
        return head.startswith(b"%PDF-")
    if content_type == "audio/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")  # EBML
    if content_type == "audio/ogg":
        return head.startswith(b"OggS")
    if content_type in ("audio/mp4", "audio/m4a", "audio/x-m4a"):
        return head[4:8] == b"ftyp"
    if content_type == "audio/aac":  # ADTS frame sync, or an ADIF header
        return head.startswith(b"ADIF") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0)
    if content_type == "audio/mpeg":  # ID3 tag, or an MPEG audio frame sync
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if content_type in ("audio/wav", "audio/x-wav", "audio/wave"):
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    return False


def normalize_purpose(value: str | None) -> str:
    purpose = (value or "generic").strip().lower()
    if not _PURPOSE_RE.match(purpose) or purpose not in PURPOSES:
        return "generic"
    return purpose


def build_key(visibility: Visibility, purpose: str, owner_id: uuid.UUID, extension: str) -> str:
    name = f"{uuid.uuid4().hex}.{extension}"
    if visibility == "public":
        now = datetime.now(UTC)
        return f"public/{purpose}/{now:%Y}/{now:%m}/{name}"
    return f"private/{purpose}/{owner_id}/{name}"


def purpose_of_key(key: str) -> str | None:
    parts = key.split("/")
    return parts[1] if len(parts) >= 3 else None
