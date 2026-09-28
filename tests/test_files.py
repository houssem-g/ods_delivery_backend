"""Uploads and signed URLs against the local MinIO (skipped when it is not running)."""

import json

import httpx
import pytest

from app.config import settings
from app.storage import s3
from tests.factories import auth, error_of

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


@pytest.fixture(scope="module", autouse=True)
def bucket():
    client = s3.server_client()
    try:
        existing = {b["Name"] for b in client.list_buckets()["Buckets"]}
    except Exception:
        pytest.skip("MinIO is not running (docker compose up -d minio)")
    if settings.S3_BUCKET not in existing:
        client.create_bucket(Bucket=settings.S3_BUCKET)
    public_read = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{settings.S3_BUCKET}/public/*"],
            }
        ],
    }
    client.put_bucket_policy(Bucket=settings.S3_BUCKET, Policy=json.dumps(public_read))


async def upload(client, user, body=PNG, content_type="image/png", visibility="public", purpose=None):
    data = {"visibility": visibility} | ({"purpose": purpose} if purpose else {})
    return await client.post(
        "/api/files/upload", files={"file": ("photo.png", body, content_type)}, data=data, headers=auth(user)
    )


async def test_public_upload_is_readable_without_auth(client, factory):
    user = await factory.user()
    response = await upload(client, user)
    assert response.status_code == 200
    url = response.json()["file_url"]
    assert "/public/generic/" in url and "file_uri" not in response.json()
    async with httpx.AsyncClient() as http:
        fetched = await http.get(url)
    assert fetched.status_code == 200 and fetched.content == PNG


async def test_private_upload_signed_for_owner_and_admin_only(client, factory):
    owner, stranger, admin = await factory.user(), await factory.user(), await factory.user(role="admin")
    response = await upload(client, owner, PDF, "application/pdf", visibility="private", purpose="receipt")
    uri = response.json()["file_uri"]
    assert uri.startswith(f"private/receipt/{owner.id}/") and "file_url" not in response.json()

    async with httpx.AsyncClient() as http:
        anonymous = await http.get(f"{settings.S3_PUBLIC_ENDPOINT_URL}/{settings.S3_BUCKET}/{uri}")
        assert anonymous.status_code == 403

        signed = await client.post(
            "/api/files/signed-url", json={"file_uri": uri, "expires_in": 60}, headers=auth(owner)
        )
        assert signed.status_code == 200
        assert (await http.get(signed.json()["signed_url"])).content == PDF

    other = await client.post("/api/files/signed-url", json={"file_uri": uri}, headers=auth(stranger))
    assert other.status_code == 403
    by_admin = await client.post("/api/files/signed-url", json={"file_uri": uri}, headers=auth(admin))
    assert by_admin.status_code == 200


async def test_courier_id_documents_are_never_signed_here(client, factory):
    courier, admin = await factory.user(), await factory.user(role="admin")
    uri = (await upload(client, courier, visibility="private", purpose="courier_id")).json()["file_uri"]
    for who in (courier, admin):
        response = await client.post("/api/files/signed-url", json={"file_uri": uri}, headers=auth(who))
        assert response.status_code == 403


async def test_signed_url_refusals(client, factory):
    user = await factory.user()
    assert (
        await client.post("/api/files/signed-url", json={"file_uri": "public/x.png"}, headers=auth(user))
    ).status_code == 400
    missing = await client.post(
        "/api/files/signed-url", json={"file_uri": "private/generic/x/y.png"}, headers=auth(user)
    )
    assert missing.status_code == 404
    assert (await client.post("/api/files/signed-url", json={"file_uri": "private/a"})).status_code == 401


async def test_upload_refusals(client, factory, monkeypatch):
    user = await factory.user()
    svg = await upload(client, user, b"<svg/>", "image/svg+xml")
    assert svg.status_code == 415 and error_of(svg) == "unsupported_media_type"
    disguised = await upload(client, user, b"<html>not a png</html>", "image/png")
    assert disguised.status_code == 415
    public_pdf = await upload(client, user, PDF, "application/pdf", visibility="public")
    assert public_pdf.status_code == 415
    assert (await upload(client, user, b"", "image/png")).status_code == 400
    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", 10)
    too_big = await upload(client, user)
    assert too_big.status_code == 413 and error_of(too_big) == "file_too_large"
    anonymous = await client.post("/api/files/upload", files={"file": ("a.png", PNG, "image/png")})
    assert anonymous.status_code == 401


async def test_unknown_purpose_falls_back_to_generic(client, factory):
    user = await factory.user()
    url = (await upload(client, user, purpose="../../etc")).json()["file_url"]
    assert "/public/generic/" in url


def test_presigned_put_is_signed_for_the_public_endpoint():
    url = s3.presign_put("private/generic/u/x.png", "image/png", 60)
    assert url.startswith(settings.S3_PUBLIC_ENDPOINT_URL) and "X-Amz-Signature" in url
