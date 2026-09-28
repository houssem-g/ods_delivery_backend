"""deleteMyAccount — erase (anonymize) the caller's account and personal data.

Answers { success: true, login_deleted: true, deleted: {counters} }; 409
{ error: 'order_in_progress', orders: [ids] } while an order is running (the front shows
its "finish or cancel first" message). What is kept, anonymized or deleted:
app/services/account_deletion.py. The session ends: the access token no longer
resolves to an active user and every refresh token is revoked.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.security.deps import CurrentUser
from app.services.account_deletion import delete_account, delete_private_objects


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    try:
        answer, keys = await delete_account(session, user)
    except ApiError as exc:
        return exc.status, exc.body()
    await session.commit()  # the account is gone before the objects are
    failures = await delete_private_objects(keys)
    if failures:
        answer["deleted"]["private_files"] -= failures
    return 200, answer
