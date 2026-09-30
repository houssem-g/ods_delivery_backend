"""Signed URLs use the addressing style the app's CSP allows (DO Spaces: bucket in the host)."""

from urllib.parse import urlparse

from app.config import settings
from app.storage import s3


def test_virtual_style_puts_the_bucket_in_the_host(monkeypatch):
    monkeypatch.setattr(settings, "S3_PUBLIC_ENDPOINT_URL", "https://fra1.digitaloceanspaces.com")
    monkeypatch.setattr(settings, "S3_BUCKET", "ods-delivery")
    monkeypatch.setattr(settings, "S3_SIGNING_ADDRESSING_STYLE", "virtual")
    s3.signing_client.cache_clear()
    try:
        url = urlparse(s3.presign_get("private/chat/a.webm", 60))
        assert url.netloc == "ods-delivery.fra1.digitaloceanspaces.com"
        assert url.path == "/private/chat/a.webm"
    finally:
        s3.signing_client.cache_clear()


def test_path_style_by_default(monkeypatch):
    monkeypatch.setattr(settings, "S3_PUBLIC_ENDPOINT_URL", "http://localhost:9110")
    monkeypatch.setattr(settings, "S3_BUCKET", "odsdlv")
    monkeypatch.setattr(settings, "S3_SIGNING_ADDRESSING_STYLE", "path")
    s3.signing_client.cache_clear()
    try:
        url = urlparse(s3.presign_get("private/x.png", 60))
        assert url.netloc == "localhost:9110" and url.path == "/odsdlv/private/x.png"
    finally:
        s3.signing_client.cache_clear()
