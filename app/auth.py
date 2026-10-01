"""Server-side Supabase Auth session and role helpers."""
from __future__ import annotations
import base64, hashlib, os, secrets, threading, time
from dataclasses import dataclass
from pathlib import Path
import httpx
from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
URL = os.getenv("SUPABASE_URL", "").rstrip("/")
ANON = os.getenv("SUPABASE_ANON_KEY", "")
SERVICE = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SECRET_KEY", "")
_refresh_lock = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}
_recent_refreshes: dict[str, tuple[float, tuple[str, str]]] = {}

@dataclass
class CurrentUser:
    id: str; email: str; display_name: str; avatar_url: str | None; role: str

def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

def _headers(key: str, token: str | None = None) -> dict[str, str]:
    h = {"apikey": key}
    if token: h["Authorization"] = f"Bearer {token}"
    elif key.startswith("eyJ"): h["Authorization"] = f"Bearer {key}"
    return h

def user_from_token(token: str) -> CurrentUser | None:
    if not URL or not ANON: return None
    with httpx.Client(timeout=10, trust_env=False) as c:
        r = c.get(f"{URL}/auth/v1/user", headers=_headers(ANON, token))
    if not r.is_success: return None
    raw = r.json(); meta = raw.get("user_metadata") or {}; email = str(raw.get("email") or "")
    role = "admin" if email.lower() in {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()} else "user"
    if SERVICE:
        payload = {"id": raw["id"], "email": email, "display_name": meta.get("full_name") or meta.get("name") or email, "avatar_url": meta.get("avatar_url") or meta.get("picture"), "role": role}
        with httpx.Client(timeout=10, trust_env=False) as c:
            c.post(f"{URL}/rest/v1/profiles?on_conflict=id", headers={**_headers(SERVICE), "Prefer":"resolution=merge-duplicates,return=representation"}, json=payload)
    return CurrentUser(raw["id"], email, str(meta.get("full_name") or meta.get("name") or email), meta.get("avatar_url") or meta.get("picture"), role)


def refresh_session(refresh_token: str) -> tuple[str, str] | None:
    """Exchange a valid Supabase refresh token for a new browser session."""
    if not URL or not ANON or not refresh_token:
        return None
    with _refresh_lock:
        cached = _recent_refreshes.get(refresh_token)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        token_lock = _refresh_locks.setdefault(refresh_token, threading.Lock())
    with token_lock:
        with _refresh_lock:
            cached = _recent_refreshes.get(refresh_token)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                response = client.post(f"{URL}/auth/v1/token?grant_type=refresh_token", headers={"apikey": ANON}, json={"refresh_token": refresh_token})
            if not response.is_success:
                return None
            payload = response.json(); access_token = payload.get("access_token"); next_refresh_token = payload.get("refresh_token")
            if not isinstance(access_token, str) or not isinstance(next_refresh_token, str):
                return None
            session = (access_token, next_refresh_token)
            with _refresh_lock:
                _recent_refreshes[refresh_token] = (time.monotonic() + 15, session)
            return session
        except (httpx.HTTPError, ValueError):
            return None

def require_authenticated(request: Request) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if not user: raise HTTPException(401, "Đăng nhập để tiếp tục.")
    return user

def require_admin(request: Request) -> CurrentUser:
    user = require_authenticated(request)
    if user.role != "admin": raise HTTPException(403, "Chỉ quản trị viên được phép.")
    return user
