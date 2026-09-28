"""S3 / MinIO / DO Spaces access. boto3 is blocking: every call runs in a thread with a timeout
(the ods-be image_service pattern), so a slow bucket never stalls the event loop."""

import asyncio
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.config import settings


class StorageUnavailable(Exception):
    pass


def _config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=5,
        read_timeout=settings.S3_TIMEOUT_SECONDS,
        retries={"max_attempts": 2, "mode": "standard"},
    )


def _client(endpoint: str) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        config=_config(),
    )


@lru_cache
def server_client() -> BaseClient:
    """Talks to the bucket from the API (docker network name in compose)."""
    return _client(settings.S3_ENDPOINT_URL)


@lru_cache
def signing_client() -> BaseClient:
    """Signs URLs for the host the browser can reach (signing is local, no network call)."""
    return _client(settings.S3_PUBLIC_ENDPOINT_URL)


async def _run(func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs), timeout=settings.S3_TIMEOUT_SECONDS + 5
        )
    except TimeoutError as exc:
        raise StorageUnavailable("object storage timed out") from exc


async def put_object(key: str, body: bytes, content_type: str) -> None:
    extra = (
        {"ACL": settings.S3_PUBLIC_OBJECT_ACL}
        if settings.S3_PUBLIC_OBJECT_ACL and key.startswith("public/")
        else {}
    )
    await _run(
        server_client().put_object,
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type,
        **extra,
    )


async def delete_object(key: str) -> None:
    await _run(server_client().delete_object, Bucket=settings.S3_BUCKET, Key=key)


async def bucket_reachable() -> bool:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(server_client().head_bucket, Bucket=settings.S3_BUCKET), timeout=3
        )
    except Exception:
        return False
    return True


def presign_get(key: str, expires_in: int) -> str:
    return signing_client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=expires_in
    )


def presign_put(key: str, content_type: str, expires_in: int) -> str:
    return signing_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )


def public_url(key: str) -> str:
    return f"{settings.public_files_base_url}/{key}"
