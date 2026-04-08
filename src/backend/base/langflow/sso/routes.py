"""Google OIDC SSO routes for Langflow.

Provides /api/v1/sso/login, /api/v1/sso/callback, and /api/v1/sso/config endpoints.
Users authenticate via Google, and are auto-provisioned in Langflow DB.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from langflow.api.utils import DbSession
from langflow.initial_setup.setup import get_or_create_default_folder
from langflow.services.database.models.user.crud import get_user_by_username
from langflow.services.database.models.user.model import User
from langflow.services.deps import get_auth_service, get_settings_service, get_variable_service

router = APIRouter(prefix="/api/v1/sso", tags=["SSO"])

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# In-memory state store (for CSRF protection). In production, use Redis or DB.
_pending_states: dict[str, float] = {}


def _get_sso_settings() -> dict:
    """Read SSO env vars. Raises if not configured."""
    import os

    client_id = os.environ.get("LANGFLOW_SSO_GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("LANGFLOW_SSO_GOOGLE_CLIENT_SECRET", "")
    allowed_domain = os.environ.get("LANGFLOW_SSO_ALLOWED_DOMAIN", "")

    if not client_id or not client_secret:
        msg = "SSO not configured. Set LANGFLOW_SSO_GOOGLE_CLIENT_ID and LANGFLOW_SSO_GOOGLE_CLIENT_SECRET."
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "allowed_domain": allowed_domain,
    }


async def _get_google_config() -> dict:
    """Fetch Google OIDC discovery document."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(GOOGLE_DISCOVERY_URL)
        resp.raise_for_status()
        return resp.json()


@router.get("/config")
async def sso_config():
    """Return SSO availability info for the frontend."""
    import os

    enabled = bool(os.environ.get("LANGFLOW_SSO_GOOGLE_CLIENT_ID"))
    return {"sso_enabled": enabled, "provider": "google"}


@router.get("/login")
async def sso_login(request: Request):
    """Redirect user to Google OAuth consent screen."""
    sso = _get_sso_settings()
    google = await _get_google_config()

    state = secrets.token_urlsafe(32)
    _pending_states[state] = datetime.now(timezone.utc).timestamp()

    # Clean up old states (> 10 min)
    now = datetime.now(timezone.utc).timestamp()
    expired = [k for k, v in _pending_states.items() if now - v > 600]
    for k in expired:
        _pending_states.pop(k, None)

    # Build redirect URI from request
    redirect_uri = str(request.url_for("sso_callback"))
    # Force https in production
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    params = {
        "client_id": sso["client_id"],
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }

    # If domain restriction, add hd param
    if sso["allowed_domain"]:
        params["hd"] = sso["allowed_domain"]

    auth_url = google["authorization_endpoint"]
    query = "&".join(f"{k}={httpx.QueryParams({k: v})}" for k, v in params.items())
    # Use httpx to build proper query string
    url = httpx.URL(auth_url).copy_merge_params(params)

    from fastapi.responses import RedirectResponse

    return RedirectResponse(str(url))


@router.get("/callback")
async def sso_callback(
    request: Request,
    response: Response,
    db: DbSession,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """Handle Google OAuth callback. Exchange code for tokens, provision user, issue JWT."""
    from fastapi.responses import RedirectResponse as Redirect

    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"OAuth error: {error}")

    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state")

    # Validate state (CSRF)
    if state not in _pending_states:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state parameter")
    _pending_states.pop(state)

    sso = _get_sso_settings()
    google = await _get_google_config()

    # Build redirect URI
    redirect_uri = str(request.url_for("sso_callback"))
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            google["token_endpoint"],
            data={
                "code": code,
                "client_id": sso["client_id"],
                "client_secret": sso["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token exchange failed: {token_resp.text}",
        )

    token_data = token_resp.json()

    # Get user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            google["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )

    if userinfo_resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to get user info")

    userinfo = userinfo_resp.json()
    email = userinfo.get("email", "")
    name = userinfo.get("name", email)

    # Domain restriction
    if sso["allowed_domain"]:
        domain = email.split("@")[-1] if "@" in email else ""
        if domain != sso["allowed_domain"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Email domain @{domain} is not allowed. Only @{sso['allowed_domain']} can sign in.",
            )

    # Find or create user in Langflow DB
    user = await get_user_by_username(db, email)
    auth_service = get_auth_service()

    if not user:
        # Auto-provision new user
        user = User(
            username=email,
            password=auth_service.get_password_hash(secrets.token_urlsafe(32)),
            is_active=True,
            is_superuser=False,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        # Create default folder
        await get_or_create_default_folder(db, user.id)

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    # Issue Langflow JWT tokens
    tokens = await auth_service.create_user_tokens(user_id=user.id, db=db, update_last_login=True)
    auth_settings = get_settings_service().auth_settings

    # Set cookies (same as normal login)
    redirect_response = Redirect("/")
    redirect_response.set_cookie(
        "refresh_token_lf",
        tokens["refresh_token"],
        httponly=auth_settings.REFRESH_HTTPONLY,
        samesite=auth_settings.REFRESH_SAME_SITE,
        secure=auth_settings.REFRESH_SECURE,
        max_age=auth_settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        domain=auth_settings.COOKIE_DOMAIN,
    )
    redirect_response.set_cookie(
        "access_token_lf",
        tokens["access_token"],
        httponly=auth_settings.ACCESS_HTTPONLY,
        samesite=auth_settings.ACCESS_SAME_SITE,
        secure=auth_settings.ACCESS_SECURE,
        max_age=auth_settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        domain=auth_settings.COOKIE_DOMAIN,
    )
    redirect_response.set_cookie(
        "apikey_tkn_lflw",
        str(user.store_api_key or ""),
        httponly=auth_settings.ACCESS_HTTPONLY,
        samesite=auth_settings.ACCESS_SAME_SITE,
        secure=auth_settings.ACCESS_SECURE,
        max_age=None,
        domain=auth_settings.COOKIE_DOMAIN,
    )

    # Initialize user variables
    await get_variable_service().initialize_user_variables(user.id, db)

    return redirect_response


def register(app) -> None:
    """Entry point for Langflow plugin system."""
    app.include_router(router)
