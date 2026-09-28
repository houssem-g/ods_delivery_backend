"""Public objects get the configured canned ACL (DO Spaces); private ones never do."""

from typing import Any

import pytest

from app.config import settings
from app.storage import s3


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.parametrize(
    ("acl", "key", "expected"),
    [
        (None, "public/shop/a.jpg", None),
        ("public-read", "public/shop/a.jpg", "public-read"),
        ("public-read", "private/receipt/a.jpg", None),
    ],
)
async def test_put_object_acl(monkeypatch, acl, key, expected) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(s3, "server_client", lambda: recorder)
    monkeypatch.setattr(settings, "S3_PUBLIC_OBJECT_ACL", acl)
    await s3.put_object(key, b"x", "image/jpeg")
    assert recorder.calls[0].get("ACL") == expected
