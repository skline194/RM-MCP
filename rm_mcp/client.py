"""Rent Manager API client with lazy auth, token cache, and pagination."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE = "https://kline.api.rentmanager.com"
TOKEN_TTL_SECONDS = 12 * 60  # under 15-min idle expiry
DEFAULT_PAGE_SIZE = 200
TOKEN_CACHE_PATH = Path(os.environ.get("RM_TOKEN_CACHE", "/tmp/rm_mcp_token.json"))

# Known UDF field IDs at unit level
UDF_LIST_FOR_LEASE = 731
UDF_ASKING_RENT = 732
UDF_LIST_ON_TENANT_TURNER = 710


class RMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RMAuthError(RMError):
    pass


class RMForbiddenError(RMError):
    pass


def _workspace_config() -> Path:
    return Path("/home/skline/.openclaw/workspace/config/rentmanager.json")


def _legacy_creds() -> Path:
    return Path.home() / ".rentmanager-credentials"


def load_credentials() -> dict[str, str]:
    """Load RM credentials from env, JSON config, or legacy export file."""
    env_user = os.environ.get("RM_USERNAME") or os.environ.get("username")
    env_pass = os.environ.get("RM_PASSWORD") or os.environ.get("password")
    env_base = os.environ.get("RM_API_BASE") or os.environ.get("base_url")
    if env_user and env_pass:
        return {
            "base_url": (env_base or DEFAULT_BASE).rstrip("/"),
            "username": env_user,
            "password": env_pass,
            "partner_token": os.environ.get("RM_PARTNER_TOKEN") or os.environ.get("partner_token") or "",
        }

    cfg = _workspace_config()
    if cfg.exists():
        data = json.loads(cfg.read_text())
        return {
            "base_url": str(data.get("base_url") or DEFAULT_BASE).rstrip("/"),
            "username": data["username"],
            "password": data["password"],
            "partner_token": data.get("partner_token") or "",
        }

    legacy = _legacy_creds()
    if legacy.exists():
        creds: dict[str, str] = {}
        for line in legacy.read_text().splitlines():
            line = line.strip()
            if not line.startswith("export ") or "=" not in line:
                continue
            key, val = line[len("export ") :].split("=", 1)
            creds[key.strip()] = val.strip().strip('"').strip("'")
        username = creds.get("username") or creds.get("RM_USERNAME")
        password = creds.get("password") or creds.get("RM_PASSWORD")
        if not username or not password:
            raise RMAuthError(f"Incomplete credentials in {legacy}")
        return {
            "base_url": (creds.get("RM_API_BASE") or DEFAULT_BASE).rstrip("/"),
            "username": username,
            "password": password,
            "partner_token": creds.get("partner_token") or creds.get("RM_PARTNER_TOKEN") or "",
        }

    raise RMAuthError(
        "No Rent Manager credentials found. Set RM_USERNAME/RM_PASSWORD or "
        "create ~/.openclaw/workspace/config/rentmanager.json"
    )


class RMClient:
    """Thin RM client. Auth is lazy and shared via on-disk token cache."""

    def __init__(self, creds: dict[str, str] | None = None):
        self.creds = creds or load_credentials()
        self.base = self.creds["base_url"].rstrip("/")
        self._token: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # --- token cache -----------------------------------------------------
    def _read_cache(self) -> str | None:
        if not TOKEN_CACHE_PATH.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        age = time.time() - float(data.get("ts") or 0)
        token = data.get("token")
        if token and age < TOKEN_TTL_SECONDS:
            return str(token)
        return None

    def _write_cache(self, token: str) -> None:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(json.dumps({"token": token, "ts": time.time()}))

    def _clear_cache(self) -> None:
        try:
            TOKEN_CACHE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self._token = None

    def _headers(self, token: str) -> dict[str, str]:
        # Canonical header casing from live RM docs/skill notes
        return {"x-rm12api-apitoken": token, "Accept": "application/json"}

    def authorize(self, *, force: bool = False) -> str:
        """Get a token only when needed.

        True lazy auth means callers should prefer request() which:
          1) tries the real command with any cached token (or none)
          2) on auth failure -> mint token
          3) retries the same command once

        authorize(force=False) returns cached token if present without probing.
        authorize(force=True) always mints a fresh token.
        """
        if not force:
            cached = self._token or self._read_cache()
            if cached:
                self._token = cached
                return cached

        payload = {
            # RM expects UserName (camel N), not Username
            "UserName": self.creds["username"],
            "Password": self.creds["password"],
        }
        r = self._session.post(
            f"{self.base}/Authentication/AuthorizeUser",
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            msg = r.text[:400]
            if "maximum number of times" in msg.lower():
                raise RMAuthError(
                    "RM session limit hit (max 10 tokens). Wait ~15 min or deauthorize old tokens.",
                    status=r.status_code,
                    body=msg,
                )
            raise RMAuthError(f"Auth failed ({r.status_code}): {msg}", status=r.status_code, body=msg)

        token = r.json()
        if isinstance(token, str):
            token = token.strip().strip('"')
        else:
            token = str(token)
        self._token = token
        self._write_cache(token)
        return token

    @staticmethod
    def _is_auth_failure(response: "requests.Response") -> bool:
        """Detect token/auth failures that should trigger re-auth + retry."""
        if response.status_code == 401:
            return True
        if response.status_code != 403:
            return False
        body = (response.text or "").lower()
        # Permission 403s are not auth failures; only token-ish 403s are.
        markers = (
            "token",
            "unauthorized",
            "not authenticated",
            "authentication",
            "apitoken",
            "api token",
            "logged in",
            "session",
        )
        return any(m in body for m in markers)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 45,
    ) -> requests.Response:
        """Lazy auth request loop:

        1) send command with current cached token if any (no pre-auth probe)
        2) if auth fails -> mint token
        3) retry same command once

        JSON body uses json_body. Multipart uploads use data + files
        (do not set Content-Type manually; requests adds the boundary).
        json_body and files are mutually exclusive.
        """
        if json_body is not None and files is not None:
            raise ValueError("json_body and files cannot both be set")
        url = f"{self.base}/{path.lstrip('/')}"

        def _do(token: str | None) -> requests.Response:
            headers = {"Accept": "application/json"}
            if token:
                headers.update(self._headers(token))
            if extra_headers:
                headers.update(extra_headers)
            # requests sets multipart Content-Type with boundary when files= is used.
            # Do not force application/json on multipart/form posts.
            return self._session.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
            )

        # Step 1: try command first (cached token if available; else bare).
        token = self._token or self._read_cache()
        if token:
            self._token = token
        r = _do(token)

        # Step 2/3: on auth failure, clear, authorize, retry once.
        if self._is_auth_failure(r):
            self._clear_cache()
            token = self.authorize(force=True)
            r = _do(token)

        if r.status_code == 401:
            raise RMAuthError(
                f"Unauthorized ({r.status_code}): {r.text[:300]}",
                status=401,
                body=r.text[:300],
            )
        if r.status_code == 403:
            raise RMForbiddenError(
                f"Forbidden ({r.status_code}): {r.text[:300]}",
                status=403,
                body=r.text[:300],
            )
        if r.status_code == 405:
            raise RMForbiddenError(
                f"Method not allowed ({r.status_code}) — account/path is effectively read-only: {r.text[:200]}",
                status=405,
                body=r.text[:200],
            )
        if r.status_code >= 400:
            raise RMError(
                f"RM API error ({r.status_code}): {r.text[:400]}",
                status=r.status_code,
                body=r.text[:400],
            )
        return r

    def get(
        self,
        path: str,
        *,
        filters: str | None = None,
        fields: str | None = None,
        embeds: str | None = None,
        pagesize: int = DEFAULT_PAGE_SIZE,
        pagenumber: int = 1,
        extra_params: dict[str, Any] | None = None,
    ) -> tuple[Any, int | None]:
        params: dict[str, Any] = {"pagesize": pagesize, "pagenumber": pagenumber}
        if filters:
            params["filters"] = filters
        if fields:
            params["fields"] = fields
        if embeds:
            params["embeds"] = embeds
        if extra_params:
            params.update(extra_params)
        r = self.request("GET", path, params=params)
        total_raw = r.headers.get("X-Total-Results")
        total = int(total_raw) if total_raw and str(total_raw).isdigit() else None
        if not r.content:
            return [], total
        return r.json(), total

    def get_all(
        self,
        path: str,
        *,
        filters: str | None = None,
        fields: str | None = None,
        embeds: str | None = None,
        pagesize: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 50,
    ) -> list[Any]:
        out: list[Any] = []
        page = 1
        while page <= max_pages:
            data, total = self.get(
                path,
                filters=filters,
                fields=fields,
                embeds=embeds,
                pagesize=pagesize,
                pagenumber=page,
            )
            if not isinstance(data, list):
                return [data] if data is not None else []
            out.extend(data)
            if total is not None and len(out) >= total:
                break
            if len(data) < pagesize:
                break
            page += 1
        return out

    def auth_status(self) -> dict[str, Any]:
        token = None
        cached = False
        try:
            existing = self._read_cache()
            if existing:
                token = existing
                cached = True
            token = self.authorize()
            ok = True
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to tool
            ok = False
            err = str(exc)
            token = self._token
        return {
            "ok": ok,
            "base_url": self.base,
            "username": self.creds.get("username"),
            "token_cached": cached,
            "token_present": bool(token),
            "token_length": len(token) if token else 0,
            "error": err,
            "notes": [
                "Lazy auth: try command first; on auth fail re-auth once and retry",
                "No pre-auth probe — token minted only after failure or force",
                "Max ~10 concurrent tokens per user",
                "Token idle expiry ~15 min; hard max ~24h",
                "Do not send partner token after AuthorizeUser",
            ],
        }
