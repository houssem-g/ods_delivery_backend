"""/api/auth/* — ARCHITECTURE §6.1. Access token in the JSON body, refresh token in the
HttpOnly cookie `odsd_refresh` (path /api/auth)."""

import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jwt import PyJWTError as JWTError
from pydantic import AliasChoices, BaseModel, Field
from slowapi.util import get_remote_address
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import ApiError
from app.integrations import google_oauth
from app.models import RevokedAccessToken, User
from app.rate_limit import ip_key, limiter
from app.security.deps import CurrentUser, bearer_token, current_user, find_user_by_email
from app.security.tokens import (
    InvalidToken,
    RefreshReused,
    clear_refresh_cookie,
    consume_refresh_token,
    decode_access_token,
    now_utc,
    revoke_refresh_token,
    set_refresh_cookie,
)
from app.services import auth as svc
from app.services import couriers, device_keys
from app.services import email_reachability as reach
from app.services import google_handoff as handoffs
from app.services.email import send_code_email

router = APIRouter(prefix="/api/auth", tags=["auth"])

AUTH_LIMIT = settings.RATE_LIMIT_AUTH
EMAIL_LIMIT = settings.RATE_LIMIT_AUTH_EMAIL
EMAIL_CHECK_LIMIT = settings.RATE_LIMIT_EMAIL_CHECK
HANDOFF_LIMIT = settings.RATE_LIMIT_AUTH_HANDOFF
NEUTRAL = {"success": True, "message": "If an account exists for this e-mail, a code was sent."}
OAUTH_NONCE_COOKIE = "odsd_oauth_nonce"


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


# Shape check only: email-validator refuses special-use domains (.local, .test) used locally.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterBody(BaseModel):
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    # « Non, mon adresse est correcte »: the person keeps an address that looked like a typo.
    keep_email: bool = Field(default=False, validation_alias=AliasChoices("keep_email", "keepEmail"))


class EmailBody(BaseModel):
    email: str = Field(max_length=320)


class CodeBody(EmailBody):
    code: str = Field(max_length=12, validation_alias=AliasChoices("code", "otp_code", "otpCode"))


class ResetBody(BaseModel):
    """{email, code, new_password}, or {reset_token, new_password} from the e-mailed link.
    A typed 6-digit code needs the e-mail address (the code alone is too short to look up)."""

    email: str | None = Field(default=None, max_length=320)
    code: str = Field(
        max_length=128, validation_alias=AliasChoices("code", "reset_token", "resetToken", "otp_code")
    )
    new_password: str = Field(
        max_length=200, validation_alias=AliasChoices("new_password", "newPassword", "password")
    )


class AccountSetupBody(EmailBody):
    code: str = Field(max_length=12, validation_alias=AliasChoices("code", "otp_code"))
    new_password: str = Field(max_length=200, validation_alias=AliasChoices("password", "new_password"))


class ChangePasswordBody(BaseModel):
    current_password: str = Field(
        default="", max_length=200, validation_alias=AliasChoices("current_password", "currentPassword")
    )
    new_password: str = Field(max_length=200, validation_alias=AliasChoices("new_password", "newPassword"))


class DeviceEnrollBody(BaseModel):
    label: str | None = Field(default=None, max_length=200)


class DeviceSignInBody(BaseModel):
    device_id: str = Field(max_length=64)
    secret: str = Field(max_length=200)


class DeviceRevokeBody(BaseModel):
    device_id: str = Field(max_length=64)


class MePatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    language: str | None = Field(default=None, pattern="^(ar|fr)$")


def _schedule(tasks: BackgroundTasks, pending: svc.PendingEmail | None) -> None:
    if pending is not None:
        tasks.add_task(
            send_code_email, pending.to, pending.purpose, pending.code, pending.language, pending.link_token
        )


def _signed_in_response(signed: svc.SignedIn, status: int = 200) -> JSONResponse:
    response = JSONResponse(signed.body, status_code=status)
    set_refresh_cookie(response, signed.refresh)
    return response


async def _refusal(session: AsyncSession, exc: svc.AuthRefusal) -> JSONResponse:
    """A refusal can still owe the user a code: commit it and send it with the error answer
    (background tasks of a raised exception would never run)."""
    await session.commit()
    tasks = BackgroundTasks()
    _schedule(tasks, exc.pending)
    return JSONResponse(exc.body(), status_code=exc.status, background=tasks)


@router.post("/login")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def login(
    request: Request, body: LoginBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    try:
        user = await svc.login(session, body.email, body.password)
    except svc.AuthRefusal as exc:
        return await _refusal(session, exc)
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    return _signed_in_response(signed)


@router.post("/register", status_code=201)
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def register(
    request: Request, body: RegisterBody, tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> Any:
    await _refuse_unreachable_new_address(session, body.email, keep_typo=body.keep_email)
    try:
        pending = await svc.register(session, body.email, body.password, body.full_name)
    except svc.AuthRefusal as exc:
        return await _refusal(session, exc)
    await session.commit()
    _schedule(tasks, pending)
    return {"success": True, "verification_required": True, "email": body.email}


async def _refuse_unreachable_new_address(session: AsyncSession, email: str, *, keep_typo: bool) -> None:
    """Sign-up (owner 19/10/2026): an address we cannot reach is said BEFORE any code is sent, and a
    frequent typo of a common provider is offered (« Vouliez-vous dire … ? »). An address already
    registered keeps its usual answers (taken, account setup): never blocked here."""
    existing = await find_user_by_email(session, email)
    if existing is not None and (
        existing.email_verified_at is not None or existing.password_hash is None or existing.deleted_at
    ):
        return
    verdict = await reach.check(email)
    if not verdict.ok:
        raise ApiError(
            422,
            "email_unreachable",
            reach.MESSAGE_UNREACHABLE,
            reason=verdict.reason,
            suggestion=verdict.suggestion,
        )
    if verdict.suggestion and not keep_typo:
        raise ApiError(422, "email_typo", reach.MESSAGE_TYPO, suggestion=verdict.suggestion)


@router.post("/check-email")
@limiter.limit(EMAIL_CHECK_LIMIT, key_func=ip_key)
async def check_email(request: Request, body: EmailBody) -> dict[str, Any]:
    """While the address is typed: {ok, reason: syntax | no_mail, suggestion}. Domain-only (no account
    is looked up): the same answer for every address of a domain."""
    return (await reach.check(body.email)).body()


@router.post("/email-status")
@limiter.limit(EMAIL_CHECK_LIMIT, key_func=ip_key)
async def email_status(
    request: Request, body: EmailBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """The code screen asks it (now and then every 15 s): did the mail server refuse this address, or
    does its domain not take mail? Only unverified accounts can carry a refusal."""
    user = await find_user_by_email(session, body.email)
    if (
        user is not None
        and user.deleted_at is None
        and user.email_verified_at is None
        and user.email_unreachable_at is not None
    ):
        return {"unreachable": True, "reason": "smtp_refused", "suggestion": reach.suggest(body.email)}
    verdict = await reach.check(body.email)
    return {"unreachable": not verdict.ok, "reason": verdict.reason, "suggestion": verdict.suggestion}


@router.post("/verify-otp")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def verify_otp(
    request: Request, body: CodeBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    try:
        user = await svc.verify_email(session, body.email, body.code)
    except ApiError:
        await session.commit()  # the failed attempt counts
        raise
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    return _signed_in_response(signed)


@router.post("/resend-otp")
@limiter.limit(EMAIL_LIMIT, key_func=ip_key)
async def resend_otp(
    request: Request, body: EmailBody, tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    # A domain that takes no mail: said instead of sending (domain-only, so no account enumeration).
    verdict = await reach.check(body.email)
    if not verdict.ok:
        raise ApiError(
            422,
            "email_unreachable",
            reach.MESSAGE_UNREACHABLE,
            reason=verdict.reason,
            suggestion=verdict.suggestion,
        )
    pending = await svc.resend_code(session, body.email)
    await session.commit()
    _schedule(tasks, pending)
    return NEUTRAL


@router.post("/reset-password-request")
@limiter.limit(EMAIL_LIMIT, key_func=ip_key)
async def reset_password_request(
    request: Request, body: EmailBody, tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    pending = await svc.request_reset(session, body.email)
    await session.commit()
    _schedule(tasks, pending)
    return NEUTRAL


async def _set_password(
    session: AsyncSession, email: str | None, code: str, new_password: str, purpose: svc.Purpose
) -> JSONResponse:
    try:
        user = await svc.set_password_with_code(session, email, code, new_password, purpose)
    except ApiError:
        await session.commit()  # the failed attempt counts
        raise
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    return _signed_in_response(signed)


@router.post("/reset-password")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def reset_password(
    request: Request, body: ResetBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    return await _set_password(session, body.email, body.code, body.new_password, "reset")


@router.post("/account-setup")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def account_setup(
    request: Request, body: AccountSetupBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """First sign-in of an account without a password: e-mailed 'migrate' code + new password."""
    return await _set_password(session, body.email, body.code, body.new_password, "migrate")


@router.post("/change-password")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def change_password(
    request: Request,
    body: ChangePasswordBody,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    row = await session.get(User, user.id)
    assert row is not None
    await svc.change_password(session, row, body.current_password, body.new_password)
    signed = await svc.sign_in(session, row)  # other sessions are revoked; this one continues
    await session.commit()
    return _signed_in_response(signed)


@router.post("/device/enroll", status_code=201)
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def device_enroll(
    request: Request,
    body: DeviceEnrollBody,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fingerprint / face sign-in: a secret for the phone's secure storage (shown once)."""
    key, secret = await device_keys.enroll(session, user.id, body.label)
    await session.commit()
    return {"device_id": str(key.id), "secret": secret}


@router.post("/device/login")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def device_login(
    request: Request, body: DeviceSignInBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    user = await device_keys.sign_in_with_key(session, body.device_id, body.secret)
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    return _signed_in_response(signed)


@router.post("/device/revoke")
async def device_revoke(
    body: DeviceRevokeBody,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    revoked = await device_keys.revoke(session, user.id, body.device_id)
    await session.commit()
    return {"success": True, "revoked": revoked}


@router.post("/refresh")
async def refresh(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not raw:
        raise ApiError(401, "refresh_missing", "No refresh token")
    try:
        consumed = await consume_refresh_token(session, raw)
    except RefreshReused:
        await session.commit()  # keep the family revocation
        return _refresh_refused("refresh_reused", "Session revoked: sign in again")
    except InvalidToken:
        return _refresh_refused("refresh_invalid", "Invalid or expired session")
    user = await session.get(User, consumed.user_id)
    if user is None or user.deleted_at is not None or user.disabled_at is not None:
        await session.rollback()
        return _refresh_refused("refresh_invalid", "Invalid or expired session")
    signed = await svc.sign_in(session, user, family=consumed.family)
    await session.commit()
    return _signed_in_response(signed)


def _refresh_refused(error: str, message: str) -> JSONResponse:
    response = JSONResponse({"error": error, "message": message}, status_code=401)
    clear_refresh_cookie(response)
    return response


async def _revoke_access_token(session: AsyncSession, token: str | None) -> None:
    """The access token of this sign-out stops working now, not at its expiry (QA 08/10): its
    jti is refused until then. Expired rows are dropped on the way (the table stays tiny)."""
    if not token:
        return
    try:
        claims = decode_access_token(token)
    except InvalidToken:
        return
    if not claims.get("jti"):
        return
    now = datetime.now(UTC)
    await session.execute(delete(RevokedAccessToken).where(RevokedAccessToken.expires_at < now))
    await session.execute(
        pg_insert(RevokedAccessToken)
        .values(jti=claims["jti"], expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC))
        .on_conflict_do_nothing(index_elements=["jti"])
    )


@router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if raw:
        user_id = await revoke_refresh_token(session, raw)
        if user_id is not None:
            await couriers.follow_session(session, user_id, online=False)
    await _revoke_access_token(session, bearer_token(request))
    await session.commit()
    response = JSONResponse({"success": True})
    clear_refresh_cookie(response)
    return response


@router.get("/me")
async def me(user: CurrentUser = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict:
    row = await session.get(User, user.id)
    assert row is not None
    return svc.serialize_me(row)


@router.patch("/me")
async def update_me(
    body: MePatch, user: CurrentUser = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict:
    row = await session.get(User, user.id)
    assert row is not None
    if body.full_name is not None:
        row.full_name = body.full_name.strip()
    if body.language is not None:
        row.language = body.language
        from app.services.device_tokens import follow_language

        await follow_language(session, row.id, body.language)  # QA 18/10 m30: the pushes follow
    await session.commit()
    await session.refresh(row)
    return svc.serialize_me(row)


# --- Google ------------------------------------------------------------------------------


def _safe_next(value: str | None) -> str:
    """Absolute URL on an allowed app origin, or a path of the main app; anything else -> the app root."""
    app_url = settings.PUBLIC_APP_URL.rstrip("/")
    if not value:
        return f"{app_url}/"
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return f"{app_url}{value}"
    parsed = urlparse(value)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = {o.rstrip("/") for o in settings.CORS_ORIGINS} | {app_url}
    if parsed.scheme in ("http", "https") and origin in allowed:
        return value
    return f"{app_url}/"


def _with_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = urlencode([*parse_qsl(parsed.query, keep_blank_values=True), *params.items()])
    return urlunparse(parsed._replace(query=query))


def _welcome_url(next_url: str | None = None) -> str:
    """The Welcome screen of the app the browser came from (an allowed origin), else the main app."""
    parsed = urlparse(_safe_next(next_url))
    return urlunparse(parsed._replace(path="/Welcome", params="", query="", fragment=""))


def _google_disabled(next_url: str | None = None) -> RedirectResponse | None:
    """Both routes are browser navigations: without Google configured, send the user back to
    Welcome with `auth_error=google_disabled` (shown there) instead of a JSON page."""
    if settings.google_enabled:
        return None
    return _redirect(_with_query(_welcome_url(next_url), {"auth_error": "google_disabled"}))


@router.get("/google/start")
async def google_start(next: str | None = None) -> Response:
    if (disabled := _google_disabled(next)) is not None:
        return disabled
    nonce = secrets.token_urlsafe(24)
    state = jwt.encode(
        {
            "type": "google_state",
            "nonce": nonce,
            "next": _safe_next(next),
            "exp": int((now_utc() + timedelta(minutes=10)).timestamp()),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    response = RedirectResponse(google_oauth.authorization_url(state), status_code=302)
    response.set_cookie(
        OAUTH_NONCE_COOKIE,
        nonce,
        max_age=600,
        path="/api/auth/google",
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Links the Google account and sends the browser back to `next` with `access_token=`
    (read by the front's app-params) + the refresh cookie."""
    if (disabled := _google_disabled()) is not None:
        return disabled
    failure = f"{settings.PUBLIC_APP_URL.rstrip('/')}/Welcome"
    try:
        claims = jwt.decode(state or "", settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return _redirect(_with_query(failure, {"auth_error": "google_state"}))
    if claims.get("type") == "google_state" and claims.get("handoff"):
        return await _google_callback_for_app(request, session, str(claims["handoff"]), code, error)
    nonce = request.cookies.get(OAUTH_NONCE_COOKIE)
    if (
        claims.get("type") != "google_state"
        or not nonce
        or not secrets.compare_digest(nonce, claims["nonce"])
    ):
        return _redirect(_with_query(failure, {"auth_error": "google_state"}))
    if error or not code:
        return _redirect(_with_query(failure, {"auth_error": "google_cancelled"}))
    try:
        profile = await google_oauth.fetch_profile(code)
        user = await svc.link_google(session, profile)
    except google_oauth.GoogleAuthError:
        return _redirect(_with_query(failure, {"auth_error": "google_failed"}))
    except ApiError as exc:
        return _redirect(_with_query(failure, {"auth_error": exc.error}))
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    target = _safe_next(claims.get("next"))
    response = _redirect(_with_query(target, {"access_token": signed.body["access_token"]}))
    set_refresh_cookie(response, signed.refresh)
    response.delete_cookie(OAUTH_NONCE_COOKIE, path="/api/auth/google")
    return response


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=302)


@router.get("/providers")
async def providers() -> dict[str, bool]:
    """Which « Continuer avec … » buttons the app shows (a provider without its keys stays hidden)."""
    return {"google": settings.google_enabled}


# --- Google from the phone app (services/google_handoff.py) ----------------------------------


class HandoffClaimBody(BaseModel):
    handoff: str = Field(max_length=64)
    secret: str = Field(max_length=128)


@router.post("/google/app-start")
@limiter.limit(AUTH_LIMIT, key_func=ip_key)
async def google_app_start(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """The app opens `authorize_url` (the phone's browser) and keeps `secret` to claim its session."""
    if not settings.google_enabled:
        raise ApiError(503, "google_disabled", "Google sign-in is not available")
    started = await handoffs.start(session, get_remote_address(request))
    await session.commit()
    state = jwt.encode(
        {
            "type": "google_state",
            "handoff": started.id,
            "exp": int((now_utc() + handoffs.TTL).timestamp()),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    return {
        "handoff": started.id,
        "secret": started.secret,
        "authorize_url": google_oauth.authorization_url(state),
        "expires_in": str(int(handoffs.TTL.total_seconds())),
    }


@router.post("/google/app-claim")
@limiter.limit(HANDOFF_LIMIT, key_func=ip_key)
async def google_app_claim(
    request: Request, body: HandoffClaimBody, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """202 {status: pending} until the browser is done; then the session (once), or {error}."""
    result = await handoffs.claim(session, body.handoff, body.secret)
    if result.state == "pending":
        return JSONResponse({"status": "pending"}, status_code=202)
    if result.state == "error":
        await session.commit()
        return JSONResponse(
            {"error": result.error, "message": "Google sign-in did not finish"}, status_code=400
        )
    user = await session.get(User, result.user_id)
    if user is None or user.deleted_at is not None or user.disabled_at is not None:
        await session.commit()
        return JSONResponse(
            {"error": "account_disabled", "message": "This account is disabled"}, status_code=403
        )
    signed = await svc.sign_in(session, user)
    await couriers.follow_session(session, user.id, online=True)
    await session.commit()
    return _signed_in_response(signed)


async def _google_callback_for_app(
    request: Request, session: AsyncSession, handoff_id: str, code: str | None, error: str | None
) -> HTMLResponse:
    row = await handoffs.open_for_callback(session, handoff_id, get_remote_address(request))
    if isinstance(row, str):
        await session.rollback()
        return _app_page(row)
    outcome = "done"
    if error or not code:
        outcome = "google_cancelled"
    else:
        try:
            profile = await google_oauth.fetch_profile(code)
            user = await svc.link_google(session, profile)
        except google_oauth.GoogleAuthError:
            outcome = "google_failed"
        except ApiError as exc:
            await session.rollback()
            row = await handoffs.open_for_callback(session, handoff_id, get_remote_address(request))
            if isinstance(row, str):
                return _app_page(row)
            outcome = exc.error
    if outcome == "done":
        handoffs.complete(row, user_id=user.id)
    else:
        handoffs.complete(row, error=outcome)
    await session.commit()
    return _app_page(outcome)


# The browser page after Google, for the phone app: FR + standard Arabic, plain words.
_APP_PAGE_TEXT = {
    "done": (
        "C’est fait !",
        "Revenez dans l’application ODS Delivery : vous y êtes connecté(e).",
        "تمّ بنجاح!",
        "يُرجى العودة إلى تطبيق ODS Delivery، فقد تمّ تسجيل دخولكم.",
    ),
    "handoff_other_device": (
        "Connexion non terminée",
        "Pour votre sécurité, terminez la connexion sur le téléphone où vous avez touché "
        "« Continuer avec Google », sur le même réseau (Wi-Fi ou données mobiles).",
        "لم يكتمل تسجيل الدخول",
        "حفاظاً على أمانكم، يُرجى إكمال تسجيل الدخول على الهاتف الذي ضغطتم فيه "
        "على « المتابعة مع Google »، وعلى الشبكة نفسها (Wi-Fi أو بيانات الهاتف).",
    ),
    "handoff_expired": (
        "Ce lien a expiré",
        "Revenez dans l’application et touchez à nouveau « Continuer avec Google ».",
        "انتهت صلاحية هذا الرابط",
        "يُرجى العودة إلى التطبيق والضغط من جديد على « المتابعة مع Google ».",
    ),
    "google_cancelled": (
        "Connexion annulée",
        "Revenez dans l’application pour réessayer ou utiliser votre e-mail.",
        "تمّ إلغاء تسجيل الدخول",
        "يُرجى العودة إلى التطبيق للمحاولة من جديد أو لاستعمال بريدكم الإلكتروني.",
    ),
    "error": (
        "La connexion Google n’a pas abouti",
        "Revenez dans l’application et réessayez, ou utilisez votre e-mail.",
        "تعذّر تسجيل الدخول عبر Google",
        "يُرجى العودة إلى التطبيق والمحاولة من جديد، أو استعمال بريدكم الإلكتروني.",
    ),
}


def _app_page(outcome: str) -> HTMLResponse:
    key = (
        outcome
        if outcome in _APP_PAGE_TEXT
        else ("handoff_expired" if outcome == "handoff_used" else "error")
    )
    fr_title, fr_text, ar_title, ar_text = (escape(t) for t in _APP_PAGE_TEXT[key])
    ok = key == "done"
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>ODS Delivery</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#070B14;color:#fff;
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px;box-sizing:border-box}}
main{{box-sizing:border-box;max-width:420px;width:100%;background:#0E1626;
border:1px solid rgba(255,255,255,.12);
border-radius:24px;padding:24px}}
.mark{{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;margin:0 auto 12px;
font-size:28px;
background:{"rgba(0,217,165,.18)" if ok else "rgba(251,191,36,.16)"};color:{"#00D9A5" if ok else "#FDE68A"}}}
h1{{font-size:20px;margin:0 0 6px;text-align:center}}
p{{font-size:15px;line-height:1.5;margin:0;color:rgba(255,255,255,.78);text-align:center}}
hr{{border:0;border-top:1px solid rgba(255,255,255,.1);margin:18px 0}}</style></head>
<body><main data-outcome="{escape(key)}"><div class="mark" aria-hidden="true">{"✓" if ok else "!"}</div>
<h1>{fr_title}</h1><p>{fr_text}</p><hr><div dir="rtl" lang="ar"><h1>{ar_title}</h1><p>{ar_text}</p></div>
</main></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
