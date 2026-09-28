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

# Purposes the front may name. Courier ID documents are signed only by the admin function.
PURPOSES = {"generic", "receipt", "issue", "hot_deal", "shop", "review", "menu", "courier_id"}
ADMIN_SIGNED_PURPOSES = {"courier_id"}
_PURPOSE_RE = re.compile(r"^[a-z_]{1,32}$")


def allowed_types(visibility: Visibility) -> dict[str, str]:
    return IMAGE_TYPES | (PRIVATE_EXTRA_TYPES if visibility == "private" else {})


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
