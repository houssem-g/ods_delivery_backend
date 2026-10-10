"""Uploads and signed URLs: back the legacy `integrations.Core.UploadFile` ({file_url}),
`UploadPrivateFile` ({file_uri}) and `CreateFileSignedUrl` ({signed_url}).

A private
file is signed only for its owner or an admin, and courier ID documents are never
signed here: only the admin function (getCourierIdPhotos) does.
"""

from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import ApiError
from app.models import File as FileRow
from app.security.deps import CurrentUser, current_user
from app.storage import keys, s3

router = APIRouter(prefix="/api/files", tags=["files"])


class SignedUrlBody(BaseModel):
    file_uri: str = Field(min_length=1, max_length=512)
    expires_in: int | None = Field(default=None, ge=1)


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    visibility: Literal["public", "private"] = Form("public"),
    purpose: str | None = Form(None),
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    clean_purpose = keys.normalize_purpose(purpose)
    extension = keys.allowed_types(visibility, clean_purpose).get(content_type)
    if extension is None:
        raise ApiError(415, "unsupported_media_type", f"File type not allowed: {content_type or 'unknown'}")
    limit = keys.max_bytes(content_type, clean_purpose, settings.UPLOAD_MAX_BYTES)
    body = await file.read(limit + 1)
    if not body:
        raise ApiError(400, "empty_file", "The file is empty")
    if len(body) > limit:
        raise ApiError(413, "file_too_large", f"The file exceeds {limit} bytes")
    if not keys.sniff_matches(content_type, body[:16]):
        raise ApiError(415, "unsupported_media_type", "The file content does not match its type")

    key = keys.build_key(visibility, clean_purpose, user.id, extension)
    try:
        await s3.put_object(key, body, content_type)
    except (s3.StorageUnavailable, BotoCoreError, ClientError) as exc:
        raise ApiError(503, "storage_unavailable", "File storage is unavailable, retry later") from exc
    session.add(
        FileRow(
            key=key,
            owner_id=user.id,
            visibility=visibility,
            purpose=clean_purpose,
            content_type=content_type,
            size_bytes=len(body),
            original_name=(file.filename or "")[:255] or None,
        )
    )
    await session.commit()
    if visibility == "public":
        return {"file_url": s3.public_url(key)}
    return {"file_uri": key}


@router.post("/signed-url")
async def signed_url(
    body: SignedUrlBody,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    if not body.file_uri.startswith("private/"):
        raise ApiError(400, "invalid_file_uri", "Not a private file URI")
    row = (await session.execute(select(FileRow).where(FileRow.key == body.file_uri))).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "File not found")
    if row.purpose in keys.ADMIN_SIGNED_PURPOSES:
        raise ApiError(403, "forbidden", "This file is only shown through the admin screens")
    if row.owner_id != user.id and not user.is_admin:
        raise ApiError(403, "forbidden", "Permission denied for this file")
    expires = min(body.expires_in or settings.SIGNED_URL_DEFAULT_SECONDS, settings.SIGNED_URL_MAX_SECONDS)
    return {"signed_url": s3.presign_get(row.key, expires)}
