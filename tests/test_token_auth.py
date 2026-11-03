"""Token reuse tests. The HTTP layer is mocked; these must not call Rent Manager."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from rm_mcp.client import (
    RMAuthError,
    RMClient,
    RMError,
    RMForbiddenError,
)

PASSWORD = "test-password"
_CREDS = {
    "base_url": "https://rm.example.test",
    "username": "user",
    "password": PASSWORD,
    "partner_token": "",
}
_ENV_KEYS = (
    "RM_USERNAME",
    "RM_PASSWORD",
    "RM_API_BASE",
    "RM_PARTNER_TOKEN",
    "RM_TOKEN_CACHE",
    "username",
    "password",
    "base_url",
    "partner_token",
)


def _header_token(headers: dict[str, Any] | None) -> str | None:
    for key, value in (headers or {}).items():
        if key.lower() == "x-rm12api-apitoken":
            return str(value)
    return None


def _reject_live(url: str) -> None:
    if "rm.example.test" not in str(url):
        raise AssertionError("refusing non-test URL")


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        text: str = "",
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json_body = json_body
        self.content = text.encode()

    def json(self) -> Any:
        if self._json_body is not None:
            return self._json_body
        return json.loads(self.text) if self.text else None


class Transport:
    """Stand-in for requests.Session methods. Counts logins and API calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.auth_calls = 0
        self.api_calls = 0
        self.on_api = None
        self.on_auth = None

    def reset_counts(self) -> None:
        with self._lock:
            self.auth_calls = 0
            self.api_calls = 0

    def install(self, client: RMClient) -> None:
        client._session.post = self.post  # type: ignore[method-assign]
        client._session.request = self.request  # type: ignore[method-assign]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        _reject_live(url)
        with self._lock:
            self.auth_calls += 1
        if self.on_auth is None:
            raise AssertionError("unexpected login")
        return self.on_auth(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        _reject_live(url)
        with self._lock:
            self.api_calls += 1
        if self.on_api is None:
            raise AssertionError("unexpected API call")
        return self.on_api(method, url, **kwargs)


def _cross_process_worker(cache_path: str, login_log: str, barrier, queue) -> None:
    """Second process: same on-disk cache, its own HTTP mock. No live calls."""
    try:
        import rm_mcp.client as client_mod

        client_mod.TOKEN_CACHE_PATH = Path(cache_path)
        client = RMClient(_CREDS)

        def post(url: str, **kwargs: Any) -> FakeResponse:
            _reject_live(url)
            with open(login_log, "a", encoding="utf-8") as handle:
                handle.write("login\n")
                handle.flush()
                os.fsync(handle.fileno())
            time.sleep(0.25)
            return FakeResponse(200, "", json_body="new-token")

        def req(method: str, url: str, **kwargs: Any) -> FakeResponse:
            _reject_live(url)
            token = _header_token(kwargs.get("headers"))
            if token == "new-token":
                return FakeResponse(200, "[]")
            barrier.wait(8)
            return FakeResponse(401, "expired token")

        client._session.post = post  # type: ignore[method-assign]
        client._session.request = req  # type: ignore[method-assign]
        client.request("GET", "/Properties")
        queue.put("ok")
    except Exception as exc:  # noqa: BLE001 - report the child failure to the parent
        queue.put(f"error:{type(exc).__name__}:{exc}")


class TokenAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "token.json"
        self._env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._path_patch = mock.patch("rm_mcp.client.TOKEN_CACHE_PATH", self.cache_path)
        self._path_patch.start()
        self.transport = Transport()

    def tearDown(self) -> None:
        self._path_patch.stop()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def make_client(self) -> RMClient:
        client = RMClient(dict(_CREDS))
        self.transport.install(client)
        return client

    def write_cache(self, token: str, ts: float | None = None) -> None:
        payload = {"token": token, "ts": time.time() if ts is None else ts}
        self.cache_path.write_text(json.dumps(payload))

    def read_token(self) -> str | None:
        if not self.cache_path.exists():
            return None
        data = json.loads(self.cache_path.read_text())
        token = data.get("token")
        return str(token) if token else None

    def test_cached_token_reused_across_calls_and_beyond_twelve_minutes(self) -> None:
        issued = 1_700_000_000.0
        self.write_cache("cached-token", ts=issued)
        clock = {"t": issued}
        self.transport.on_api = lambda method, url, **kwargs: (
            self.assertEqual(_header_token(kwargs.get("headers")), "cached-token")
            or FakeResponse(200, "[]")
        )
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("login")
        )

        with mock.patch("rm_mcp.client.time.time", lambda: clock["t"]):
            client = self.make_client()
            for _ in range(20):
                client.request("GET", "/Properties")
            # Still in use, well past the old 12-minute client timer and the
            # 15-minute idle window. The cache must not be thrown away.
            for _ in range(4):
                clock["t"] += 13 * 60
                client.request("GET", "/Properties")
            clock["t"] = issued + 48 * 60 * 60
            restarted = self.make_client()
            restarted.request("GET", "/Units")

        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(self.transport.api_calls, 25)
        self.assertEqual(self.read_token(), "cached-token")
        self.assertNotIn(PASSWORD, self.cache_path.read_text())

    def test_auth_failure_logs_in_once_and_retries_once(self) -> None:
        self.write_cache("old-token", ts=1_700_000_000.0)
        seen: list[str | None] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            token = _header_token(kwargs.get("headers"))
            seen.append(token)
            self.assertIn("/Properties", url)
            if token == "old-token":
                return FakeResponse(401, "token expired")
            return FakeResponse(200, "[]")

        def auth(url: str, **kwargs: Any) -> FakeResponse:
            self.assertIn("/Authentication/AuthorizeUser", url)
            self.assertEqual(kwargs["json"]["UserName"], "user")
            self.assertEqual(kwargs["json"]["Password"], PASSWORD)
            return FakeResponse(200, "", json_body="new-token")

        self.transport.on_api = api
        self.transport.on_auth = auth
        response = self.make_client().request("GET", "/Properties")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["old-token", "new-token"])
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 2)
        self.assertEqual(self.read_token(), "new-token")
        cached = self.cache_path.read_text()
        self.assertNotIn(PASSWORD, cached)
        self.assertEqual(self.cache_path.stat().st_mode & 0o777, 0o600)

    def test_token_403_logs_in_once_and_retries_once(self) -> None:
        self.write_cache("old-token")
        seen: list[str | None] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            token = _header_token(kwargs.get("headers"))
            seen.append(token)
            if token == "old-token":
                return FakeResponse(403, "Invalid API token")
            return FakeResponse(200, "[]")

        self.transport.on_api = api
        self.transport.on_auth = lambda url, **kwargs: FakeResponse(200, "", json_body="new-token")
        response = self.make_client().request("GET", "/Tenants")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["old-token", "new-token"])
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 2)

    def test_second_failure_after_relogin_does_not_login_again(self) -> None:
        self.write_cache("old-token")
        self.transport.on_api = lambda method, url, **kwargs: FakeResponse(401, "still unauthorized")
        self.transport.on_auth = lambda url, **kwargs: FakeResponse(200, "", json_body="new-token")
        client = self.make_client()

        with self.assertRaises(RMAuthError) as caught:
            client.request("GET", "/Properties")

        message = str(caught.exception)
        self.assertIn("Unauthorized", message)
        self.assertNotIn(PASSWORD, message)
        self.assertNotIn("new-token", message)
        self.assertNotIn("old-token", message)
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 2)
        self.assertEqual(self.read_token(), "new-token")

    def test_missing_token_logs_in_once_on_a_real_call(self) -> None:
        seen: list[str | None] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            token = _header_token(kwargs.get("headers"))
            seen.append(token)
            if token is None:
                return FakeResponse(401, "not authenticated")
            return FakeResponse(200, "[]")

        self.transport.on_api = api
        self.transport.on_auth = lambda url, **kwargs: FakeResponse(200, "", json_body="minted-token")
        response = self.make_client().request("GET", "/Vendors")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [None, "minted-token"])
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 2)
        self.assertEqual(self.read_token(), "minted-token")

    def test_permission_denied_and_server_error_do_not_login(self) -> None:
        self.write_cache("cached-token")
        client = self.make_client()

        self.transport.on_api = lambda method, url, **kwargs: FakeResponse(
            403, "Insufficient privileges"
        )
        with self.assertRaises(RMForbiddenError):
            client.request("GET", "/Properties")

        self.transport.on_api = lambda method, url, **kwargs: FakeResponse(500, "server error")
        with self.assertRaises(RMError) as caught:
            client.request("GET", "/Properties")
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIsInstance(caught.exception, RMAuthError)
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(self.transport.api_calls, 2)

    def test_login_limit_message_is_daily(self) -> None:
        self.transport.on_api = lambda method, url, **kwargs: FakeResponse(401, "no token")
        self.transport.on_auth = lambda url, **kwargs: FakeResponse(
            400,
            "You have reached the maximum number of times you can log in",
        )
        with self.assertRaises(RMAuthError) as caught:
            self.make_client().request("GET", "/Properties")

        message = str(caught.exception)
        self.assertIn("per day", message)
        self.assertNotIn("15 min", message)
        self.assertNotIn("~15", message)
        self.assertNotIn(PASSWORD, message)
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 1)

    def test_status_tools_do_not_login_when_no_token_is_cached(self) -> None:
        self.assertFalse(self.cache_path.exists())
        self.transport.on_api = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("API call")
        )
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("login")
        )
        client = self.make_client()
        status = client.auth_status()
        self.assertFalse(status["ok"])
        self.assertFalse(status["token_cached"])
        self.assertFalse(status["token_present"])
        self.assertEqual(status["token_length"], 0)
        self.assertIn("does not log in", status["error"])
        notes = " ".join(status["notes"]).lower()
        self.assertIn("per day", notes)
        self.assertNotIn("wait", notes)
        self.assertNotIn("concurrent", notes)

        import rm_mcp.server as server

        server._client = client
        try:
            raw = json.loads(server.rm_auth_status())
            self.assertTrue(raw["ok"])
            self.assertFalse(raw["data"]["token_present"])
            self.assertFalse(raw["data"]["token_cached"])
            self.assertNotIn(PASSWORD, json.dumps(raw))

            who = json.loads(server.rm_whoami_capabilities())
            self.assertTrue(who["ok"])
            self.assertFalse(who["data"]["auth"]["token_present"])
            self.assertGreaterEqual(len(who["data"]["reads"]), 12)
            for info in who["data"]["reads"].values():
                self.assertTrue(info["skipped"])
                self.assertFalse(info["ok"])
            self.assertNotIn(PASSWORD, json.dumps(who))
        finally:
            server._client = None

        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(self.transport.api_calls, 0)

    def test_whoami_probes_cached_token_and_does_not_relogin(self) -> None:
        self.write_cache("cached-token", ts=1_700_000_000.0)
        self.transport.on_api = lambda method, url, **kwargs: (
            self.assertEqual(_header_token(kwargs.get("headers")), "cached-token")
            or FakeResponse(200, "[]", headers={"X-Total-Results": "3"})
        )
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("login")
        )
        client = self.make_client()

        import rm_mcp.server as server

        server._client = client
        try:
            who = json.loads(server.rm_whoami_capabilities())
        finally:
            server._client = None

        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(self.transport.api_calls, 12)
        for info in who["data"]["reads"].values():
            self.assertTrue(info["ok"])
            self.assertEqual(info["status"], 200)
            self.assertEqual(info["total"], "3")

        self.transport.reset_counts()
        self.transport.on_api = lambda method, url, **kwargs: FakeResponse(401, "token expired")
        server._client = client
        try:
            rejected = json.loads(server.rm_whoami_capabilities())
        finally:
            server._client = None

        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(self.transport.api_calls, 12)
        for info in rejected["data"]["reads"].values():
            self.assertFalse(info["ok"])
            self.assertEqual(info["status"], 401)
        self.assertEqual(self.read_token(), "cached-token")

    def test_two_threads_login_once(self) -> None:
        self.write_cache("old-token")
        barrier = threading.Barrier(2)
        client = self.make_client()

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            token = _header_token(kwargs.get("headers"))
            if token == "new-token":
                return FakeResponse(200, "[]")
            barrier.wait(5)
            return FakeResponse(401, "expired token")

        def auth(url: str, **kwargs: Any) -> FakeResponse:
            time.sleep(0.1)
            return FakeResponse(200, "", json_body="new-token")

        self.transport.on_api = api
        self.transport.on_auth = auth
        errors: list[BaseException] = []

        def run() -> None:
            try:
                client.request("GET", "/Properties")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(self.transport.api_calls, 4)
        self.assertEqual(self.read_token(), "new-token")

    def test_two_processes_login_once(self) -> None:
        self.write_cache("old-token", ts=1_700_000_000.0)
        login_log = Path(self.tmp.name) / "logins.txt"
        login_log.write_text("")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_cross_process_worker,
                args=(str(self.cache_path), str(login_log), barrier, queue),
            )
            for _ in range(2)
        ]
        try:
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(10)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(2)

        for proc in procs:
            self.assertFalse(proc.is_alive())
            self.assertEqual(proc.exitcode, 0)
        results = [queue.get(timeout=5) for _ in procs]
        self.assertEqual(sorted(results), ["ok", "ok"])
        self.assertEqual(login_log.read_text().count("login"), 1)
        self.assertEqual(self.read_token(), "new-token")
        self.assertNotIn(PASSWORD, self.cache_path.read_text())


if __name__ == "__main__":
    unittest.main()
