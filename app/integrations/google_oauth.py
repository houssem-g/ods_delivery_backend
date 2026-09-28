"""Google OAuth 2.0 authorization-code flow (userinfo endpoint; no local id_token verification needed
because the token comes straight from Google over TLS in the code exchange)."""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import settings

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
TIMEOUT = httpx.Timeout(10.0)


class GoogleAuthError(Exception):
    pass


@dataclass(frozen=True)
class GoogleProfile:
    sub: str
    email: str
    email_verified: bool
    name: str


def authorization_url(state: str) -> str:
    query = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def http_client() -> httpx.AsyncClient:
    """Indirection point for tests (they swap in an httpx.MockTransport)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


async def fetch_profile(code: str) -> GoogleProfile:
    async with http_client() as client:
        token = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if token.status_code != 200 or "access_token" not in token.json():
            raise GoogleAuthError(f"code exchange failed ({token.status_code})")
        info = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"}
        )
        if info.status_code != 200:
            raise GoogleAuthError(f"userinfo failed ({info.status_code})")
        data = info.json()
    if not data.get("sub") or not data.get("email"):
        raise GoogleAuthError("incomplete profile")
    return GoogleProfile(
        sub=str(data["sub"]),
        email=str(data["email"]).strip(),
        email_verified=bool(data.get("email_verified")),
        name=str(data.get("name") or ""),
    )
