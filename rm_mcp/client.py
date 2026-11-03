"""Rent Manager API client with lazy auth, token cache, and pagination."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("rm-mcp.client")

# Extra tries after the first GET. 429 / 502 / 503 / 504 and timeouts only.
# POST and DELETE are never retried here. A 401/403 still follows the
# one-login path in request() and does not use this loop.
GET_TRANSIENT_RETRIES = 3
GET_RETRY_STATUSES = frozenset({429, 502, 503, 504})
GET_RETRY_BACKOFF_SECONDS = 0.5
# A huge Retry-After must not stall a tool call for hours.
GET_RETRY_AFTER_CAP_SECONDS = 30.0

# Field,op,value clauses are separated by ';'. Commas separate the three
# parts. Parentheses wrap in(...) and bt(...) value lists.
_FILTER_DELIMITERS = re.compile(r"[,;()]+")

DEFAULT_BASE = "https://kline.api.rentmanager.com"
DEFAULT_PAGE_SIZE = 200
# Shared across processes. `ts` is when the token was minted, for humans only.
# Rent Manager expires tokens (about 24h, or 15 minutes idle). This client does
# not: a cached token is sent until Rent Manager rejects it.
TOKEN_CACHE_PATH = Path(os.environ.get("RM_TOKEN_CACHE", "/tmp/rm_mcp_token.json"))

# Threads in this process, plus flock so another process sharing the cache
# cannot AuthorizeUser at the same time.
_AUTH_THREAD_LOCK = threading.Lock()


def _lock_path() -> Path:
    return TOKEN_CACHE_PATH.with_name(TOKEN_CACHE_PATH.name + ".lock")


@contextmanager
def _exclusive_cache_lock() -> Iterator[None]:
    """Hold the shared token lock across a cache re-read and at most one login.

    Released before the retried API call. If this process dies, the OS drops
    the flock when the fd closes, so the lock cannot stick.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    thread_locked = False
    locked = False
    try:
        try:
            _AUTH_THREAD_LOCK.acquire()
            thread_locked = True
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
    finally:
        if thread_locked:
            _AUTH_THREAD_LOCK.release()


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


class PageList(list):
    """Rows from get_all, plus whether the download stopped early.

    Behaves as a list so existing callers can iterate. `meta` is what list
    tools copy into the JSON they hand back to the model.
    """

    def __init__(self, items: list[Any], *, truncated: bool, total: int | None):
        super().__init__(items)
        self.truncated = bool(truncated)
        self.total = total

    @property
    def meta(self) -> dict[str, Any]:
        return {"truncated": self.truncated, "total": self.total}


def sanitize_filter_text(value: Any) -> str:
    """Make user text safe to drop into a single Rent Manager filter value.

    Commas, semicolons, and parentheses are filter syntax. They are removed
    so a name cannot add or rewrite a clause. If the text was not empty but
    nothing is left, raise — an empty value would match everything.
    """
    raw = str(value or "").strip()
    cleaned = _FILTER_DELIMITERS.sub("", raw).strip()
    if raw and not cleaned:
        raise ValueError(
            "Search text is only filter punctuation (commas, semicolons, or parentheses) "
            "and was rejected so it cannot change the filter."
        )
    return cleaned


def response_total(response: Any) -> int | None:
    """X-Total-Results as an int, or None when the header is missing."""
    headers = getattr(response, "headers", None) or {}
    raw = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        raw = getter("X-Total-Results")
    if raw is None:
        for key, value in headers.items():
            if str(key).lower() == "x-total-results":
                raw = value
                break
    if raw is None:
        return None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None


def _retry_after_seconds(response: Any) -> float | None:
    """Seconds to wait from a Retry-After header, capped. None if unusable."""
    headers = getattr(response, "headers", None) or {}
    raw = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        if seconds < 0:
            return None
        return min(seconds, GET_RETRY_AFTER_CAP_SECONDS)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    if delay < 0:
        return 0.0
    return min(delay, GET_RETRY_AFTER_CAP_SECONDS)


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
        """Return the on-disk token, whatever its age. Never expires it locally."""
        if not TOKEN_CACHE_PATH.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        token = data.get("token")
        if not token:
            return None
        return str(token)

    def _write_cache(self, token: str) -> None:
        """Write the token atomically. Caller must hold the cache lock."""
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"token": token, "ts": time.time()})
        tmp = TOKEN_CACHE_PATH.with_name(TOKEN_CACHE_PATH.name + ".tmp")
        tmp.write_text(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, TOKEN_CACHE_PATH)

    def _headers(self, token: str) -> dict[str, str]:
        # X-RM12Api-ApiToken. This lowercase form is what works live; HTTP
        # header names are case-insensitive.
        return {"x-rm12api-apitoken": token, "Accept": "application/json"}

    def _mint_token(self) -> str:
        """POST /Authentication/AuthorizeUser a single time. Caller holds the lock."""
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
                    "RM login limit hit (max 10 AuthorizeUser logins per day). "
                    "Do not retry in a loop; the limit resets the next day.",
                    status=r.status_code,
                    body=msg,
                )
            raise RMAuthError(f"Auth failed ({r.status_code}): {msg}", status=r.status_code, body=msg)

        token = r.json()
        if isinstance(token, str):
            token = token.strip().strip('"')
        else:
            token = str(token)
        if not token:
            raise RMAuthError(
                "Auth response did not include a token",
                status=r.status_code,
                body=r.text[:400],
            )
        self._token = token
        self._write_cache(token)
        return token

    def _token_after_auth_failure(self, used_token: str | None) -> str:
        """Reuse a token another process just minted, or mint once.

        Called only after an auth failure. The lock is held through the mint
        so a second process waits, re-reads, and does not log in again.
        """
        with _exclusive_cache_lock():
            fresh = self._read_cache()
            if fresh and fresh != used_token:
                self._token = fresh
                return fresh
            self._token = None
            return self._mint_token()

    def authorize(self, *, force: bool = False) -> str:
        """Return the cached token, or mint one when the cache is empty.

        Does not discard a token because of its age. force=False does not call
        AuthorizeUser when a token is already cached. force=True always mints.
        API calls should use request(), which logs in at most once after a real
        auth failure and retries that call once.
        """
        if not force:
            cached = self._token or self._read_cache()
            if cached:
                self._token = cached
                return cached
        with _exclusive_cache_lock():
            if not force:
                cached = self._read_cache()
                if cached:
                    self._token = cached
                    return cached
            self._token = None
            return self._mint_token()

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
        allow_reauth: bool = True,
    ) -> requests.Response:
        """Call Rent Manager with the cached token. Log in at most once.

        1) Send the call with the cached token (or with no token, if none is cached).
        2) If that succeeds, return it and leave the cache alone.
        3) If it fails because there is no token or the token was rejected,
           mint one new token (or reuse one another process just wrote) and
           retry this same call once.
        4) If the retry fails, raise. Do not log in again.

        allow_reauth=False reports an auth failure without calling AuthorizeUser.
        Status checks use that so they cannot spend a daily login.

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

        def _send(token: str | None) -> requests.Response:
            if method.upper() != "GET":
                return _do(token)
            return self._get_with_transient_retries(lambda: _do(token), path)

        # This process's token, otherwise the on-disk cache (survives restarts).
        # Age is ignored. After an auth failure the lock path re-reads disk so
        # a token another process just minted is reused instead of a second login.
        token = self._token or self._read_cache()
        if token:
            self._token = token
        r = _send(token)

        # One re-auth and one retry. A second auth failure falls through and raises.
        # Transient 429/5xx retries never reach this branch and never log in.
        if allow_reauth and self._is_auth_failure(r):
            token = self._token_after_auth_failure(token)
            r = _send(token)

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

    def _pause_before_get_retry(self, attempt: int, response: Any) -> None:
        """Wait before another GET. Retry-After wins over the backoff schedule."""
        retry_after = _retry_after_seconds(response) if response is not None else None
        if retry_after is None:
            delay = GET_RETRY_BACKOFF_SECONDS * (2**attempt)
        else:
            delay = retry_after
        time.sleep(delay)

    def _get_with_transient_retries(self, send: Any, path: str) -> requests.Response:
        """Retry a GET on throttle, gateway errors, and timeouts. Never logs in."""
        response: requests.Response | None = None
        for attempt in range(GET_TRANSIENT_RETRIES + 1):
            try:
                response = send()
            except requests.Timeout:
                if attempt >= GET_TRANSIENT_RETRIES:
                    raise RMError(
                        "Rent Manager GET timed out after "
                        f"{GET_TRANSIENT_RETRIES + 1} tries. The login was not retried."
                    ) from None
                log.info(
                    "Rent Manager GET %s timed out; retrying (%s of %s)",
                    path,
                    attempt + 1,
                    GET_TRANSIENT_RETRIES,
                )
                self._pause_before_get_retry(attempt, None)
                continue
            if response.status_code in GET_RETRY_STATUSES and attempt < GET_TRANSIENT_RETRIES:
                log.info(
                    "Rent Manager GET %s returned %s; retrying (%s of %s)",
                    path,
                    response.status_code,
                    attempt + 1,
                    GET_TRANSIENT_RETRIES,
                )
                self._pause_before_get_retry(attempt, response)
                continue
            return response
        if response is not None:
            return response
        raise RMError("Rent Manager GET failed after retries. The login was not retried.")

    def get(
        self,
        path: str,
        *,
        filters: str | None = None,
        fields: str | None = None,
        embeds: str | None = None,
        orderby: str | None = None,
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
        # The explicit argument wins over extra_params so callers can rely on it.
        if orderby:
            params["orderby"] = orderby
        r = self.request("GET", path, params=params)
        total = response_total(r)
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
        orderby: str | None = None,
        pagesize: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 50,
    ) -> PageList:
        """Page until the end, or until max_pages. The result says if it stopped early."""
        out: list[Any] = []
        total: int | None = None
        pages = 0
        while pages < max_pages:
            pages += 1
            data, page_total = self.get(
                path,
                filters=filters,
                fields=fields,
                embeds=embeds,
                orderby=orderby,
                pagesize=pagesize,
                pagenumber=pages,
            )
            if page_total is not None:
                total = page_total
            if not isinstance(data, list):
                items = [data] if data is not None else []
                return PageList(items, truncated=False, total=total)
            out.extend(data)
            if total is not None and len(out) >= total:
                return PageList(out, truncated=False, total=total)
            if len(data) < pagesize:
                short = total is not None and len(out) < total
                return PageList(out, truncated=short, total=total)
            if pages >= max_pages:
                break
        truncated = True
        if total is not None and len(out) >= total:
            truncated = False
        return PageList(out, truncated=truncated, total=total)

    def auth_status(self) -> dict[str, Any]:
        """Report the cached token. Does not call Rent Manager or log in."""
        cached = self._read_cache()
        token = cached or self._token
        notes = [
            "Cached token is reused until Rent Manager rejects it. This client does not expire tokens on a timer.",
            "On auth failure, log in once and retry that call once. Never loop logins.",
            "Rent Manager allows about 10 logins (POST /Authentication/AuthorizeUser) per day.",
            "Rent Manager may expire a token after 24 hours or about 15 minutes idle; the server decides.",
            "Do not send partner token after AuthorizeUser",
        ]
        if not token:
            return {
                "ok": False,
                "base_url": self.base,
                "username": self.creds.get("username"),
                "token_cached": False,
                "token_present": False,
                "token_length": 0,
                "error": "No cached token. This check does not log in.",
                "notes": notes,
            }
        return {
            "ok": True,
            "base_url": self.base,
            "username": self.creds.get("username"),
            "token_cached": bool(cached),
            "token_present": True,
            "token_length": len(token),
            "error": None,
            "notes": notes,
        }
