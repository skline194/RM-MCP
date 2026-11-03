"""Data-correctness tests. The HTTP layer is mocked; these must not call Rent Manager."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import requests

from rm_mcp.client import (
    GET_RETRY_AFTER_CAP_SECONDS,
    GET_TRANSIENT_RETRIES,
    RMClient,
    RMError,
    sanitize_filter_text,
)

PASSWORD = "test-password"
TOKEN = "cached-token"
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
        raise AssertionError(f"refusing non-test URL: {url}")


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        *,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        if text is None:
            raw = "" if body is None else json.dumps(body)
        else:
            raw = text
        self.status_code = status_code
        self.text = raw
        self.headers = headers or {}
        self._body = body
        self.content = raw.encode()

    def json(self) -> Any:
        if self._body is not None:
            return self._body
        return json.loads(self.text) if self.text else None


class Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.auth_calls = 0
        self.on_api = None
        self.on_auth = None

    def install(self, client: RMClient) -> None:
        client._session.post = self.post  # type: ignore[method-assign]
        client._session.request = self.request  # type: ignore[method-assign]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        _reject_live(url)
        self.auth_calls += 1
        self.calls.append({"method": "POST", "url": url, "kwargs": kwargs})
        if self.on_auth is None:
            raise AssertionError(f"unexpected login: {url}")
        return self.on_auth(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        _reject_live(url)
        self.calls.append({"method": method.upper(), "url": url, "kwargs": kwargs})
        if self.on_api is None:
            raise AssertionError(f"unexpected API call: {method} {url}")
        return self.on_api(method, url, **kwargs)

    @property
    def api_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] != "POST" or "AuthorizeUser" not in call["url"]]


class DataCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "token.json"
        self._env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._path_patch = mock.patch("rm_mcp.client.TOKEN_CACHE_PATH", self.cache_path)
        self._path_patch.start()
        self.transport = Transport()
        self.sleeps: list[float] = []
        self._sleep_patch = mock.patch(
            "rm_mcp.client.time.sleep",
            side_effect=lambda delay: self.sleeps.append(delay),
        )
        self._sleep_patch.start()
        import rm_mcp.server as server

        self.server = server
        self._saved_client = server._client
        self._saved_udf = server._udf_cache
        self._saved_udf_source = server._udf_source
        server._client = None
        server._udf_cache = None
        server._udf_source = None

    def tearDown(self) -> None:
        self.server._client = self._saved_client
        self.server._udf_cache = self._saved_udf
        self.server._udf_source = self._saved_udf_source
        self._sleep_patch.stop()
        self._path_patch.stop()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def make_client(self, *, token: str | None = TOKEN) -> RMClient:
        client = RMClient(dict(_CREDS))
        self.transport.install(client)
        if token is not None:
            self.cache_path.write_text(json.dumps({"token": token, "ts": 1}))
            client._token = token
        self.server._client = client
        return client

    def test_filter_text_strips_delimiters_and_rejects_punctuation_only(self) -> None:
        self.assertEqual(sanitize_filter_text("Smith, Jr."), "Smith Jr.")
        self.assertEqual(sanitize_filter_text("Oak;IsActive,eq,false"), "OakIsActiveeqfalse")
        self.assertEqual(sanitize_filter_text("Oak (West)"), "Oak West")
        self.assertEqual(sanitize_filter_text("  in,(1,2)  "), "in12")
        self.assertEqual(sanitize_filter_text(""), "")
        self.assertEqual(sanitize_filter_text(None), "")
        for raw in (",,;", ";()", "(,)"):
            with self.assertRaises(ValueError):
                sanitize_filter_text(raw)
        cleaned = sanitize_filter_text("a,b;c(d)e")
        for delimiter in ",;()":
            self.assertNotIn(delimiter, cleaned)

    def test_property_query_cannot_extend_the_filter(self) -> None:
        seen: list[dict[str, Any]] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertIn("/Properties", url)
            seen.append(kwargs["params"])
            return FakeResponse(
                200,
                body=[
                    {"PropertyID": 1, "Name": "Oak", "IsActive": True},
                    {"PropertyID": 2, "Name": "Elm", "IsActive": True},
                ],
                headers={"X-Total-Results": "2"},
            )

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_find_property("Oak;IsActive,eq,false", limit=1))

        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(seen[0]["filters"], "Name,ct,OakIsActiveeqfalse;IsActive,eq,true")
        self.assertNotIn("eq,false", seen[0]["filters"])
        self.assertTrue(raw["ok"])
        self.assertEqual(len(raw["data"]), 1)
        self.assertTrue(raw["meta"]["truncated"])
        self.assertEqual(raw["meta"]["total"], 2)
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertNotIn(PASSWORD, json.dumps(raw))
        self.assertNotIn(TOKEN, json.dumps(raw))

    def test_punctuation_only_query_does_not_call_the_api(self) -> None:
        self.make_client()
        self.transport.on_api = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API"))
        raw = json.loads(self.server.rm_find_property(",,;"))
        self.assertFalse(raw["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_name_filters_are_sanitized_on_the_other_search_tools(self) -> None:
        filters: list[str] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            params = kwargs.get("params") or {}
            filters.append(str(params.get("filters") or params.get("filterExpression") or ""))
            if url.endswith("/Units/Addresses"):
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            if "/Units" in url:
                return FakeResponse(
                    200,
                    body=[{"UnitID": 3, "Name": "A,B"}],
                    headers={"X-Total-Results": "1"},
                )
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))

        self.server.rm_list_prospects(query="Ann;Status,eq,Lost")
        self.server.rm_list_vendors(query="Ace, Inc;IsActive,eq,false")
        self.server.rm_find_tenant(query="Ada;Status,eq,Past", property_id=4)
        self.server.rm_unit_detail(unit_name="A;B,C(D)")
        self.server.rm_search_bills(reference="INV;Amount,eq,0", reference_contains="x,(y)", due_date="2024-01-01;Hack,eq,1")

        self.assertIn("Name,ct,AnnStatuseqLost", filters)
        self.assertTrue(any(item.startswith("Name,ct,Ace Inc") and "IsActive,eq,false" not in item for item in filters))
        self.assertTrue(any(item.startswith("Name,ct,AdaStatuseqPast") or "Name,ct,AdaStatuseqPast" in item for item in filters))
        self.assertIn("Name,eq,ABCD", filters)
        # The name Rent Manager returned still has a comma; the follow-up filter must not.
        self.assertIn("Unit.Name,eq,AB", filters)
        bill = next(item for item in filters if item.startswith("AccountID") or "Reference,eq," in item)
        self.assertIn("Reference,eq,INVAmounteq0", bill)
        self.assertIn("Reference,ct,xy", bill)
        self.assertIn("DueDate,eq,2024-01-01", bill)
        self.assertNotIn("Hack", bill)
        for item in filters:
            value_side = item.split(",ct,", 1)[-1] if ",ct," in item else item
            # User-controlled clauses cannot introduce a second operator comma inside the value
            # beyond the clauses this server itself joins with ';'.
            self.assertNotIn(",eq,false", item)
            self.assertNotIn("(y)", value_side)

    def test_get_all_reports_a_page_cutoff_and_the_header_total(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            page = int(kwargs["params"]["pagenumber"])
            self.assertEqual(kwargs["params"]["orderby"], "TransactionDate DESC")
            if page == 1:
                return FakeResponse(200, body=[{"id": 1}, {"id": 2}], headers={"X-Total-Results": "10"})
            raise AssertionError("should stop at max_pages")

        client = self.make_client()
        self.transport.on_api = api
        rows = client.get_all("/Payments", orderby="TransactionDate DESC", pagesize=2, max_pages=1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows.truncated)
        self.assertEqual(rows.total, 10)
        self.assertEqual(rows.meta, {"truncated": True, "total": 10})

    def test_get_all_is_complete_when_the_header_total_is_reached(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            page = int(kwargs["params"]["pagenumber"])
            self.assertEqual(kwargs["params"]["orderby"], "Name")
            if page == 1:
                return FakeResponse(200, body=[{"id": 1}, {"id": 2}], headers={"X-Total-Results": "3"})
            return FakeResponse(200, body=[{"id": 3}], headers={"x-total-results": "3"})

        client = self.make_client()
        self.transport.on_api = api
        rows = client.get_all("/Vendors", orderby="Name", pagesize=2, max_pages=5)
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertFalse(rows.truncated)
        self.assertEqual(rows.total, 3)

    def test_get_all_flags_a_full_page_when_no_total_was_sent(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(200, body=[{"id": 1}, {"id": 2}])

        client = self.make_client()
        self.transport.on_api = api
        rows = client.get_all("/Notes", pagesize=2, max_pages=1)
        self.assertTrue(rows.truncated)
        self.assertIsNone(rows.total)

        self.transport.calls.clear()

        def short(method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(200, body=[{"id": 1}])

        self.transport.on_api = short
        done = client.get_all("/Notes", pagesize=2, max_pages=5)
        self.assertFalse(done.truncated)
        self.assertIsNone(done.total)
        self.assertEqual(len(self.transport.calls), 1)

    def test_list_tool_says_when_paging_stops_early(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertIn("/Notes", url)
            page = int(kwargs["params"]["pagenumber"])
            self.assertEqual(int(kwargs["params"]["pagesize"]), 50)
            self.assertLessEqual(page, 2)
            rows = [{"NoteID": page * 100 + i, "Name": "n", "Description": "d"} for i in range(50)]
            return FakeResponse(200, body=rows, headers={"X-Total-Results": "500"})

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_list_notes(limit=20))
        self.assertTrue(raw["ok"])
        self.assertEqual(len(raw["data"]), 20)
        self.assertTrue(raw["meta"]["truncated"])
        self.assertEqual(raw["meta"]["total"], 500)
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertIn("500", raw["meta"]["notice"])
        self.assertEqual(len(self.transport.calls), 2)

    def test_banks_report_a_header_total_bigger_than_the_body(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertIn("/Banks", url)
            return FakeResponse(
                200,
                body=[{"BankID": 1, "Name": "Operating"}, {"BankID": 2, "Name": "Reserve"}],
                headers={"X-Total-Results": "10"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_list_banks(limit=50))
        self.assertEqual(len(raw["data"]), 2)
        self.assertTrue(raw["meta"]["truncated"])
        self.assertEqual(raw["meta"]["total"], 10)
        self.assertIn("CUT OFF", raw["meta"]["notice"])

    def test_payments_are_ordered_by_the_server(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertIn("/Payments", url)
            params = kwargs["params"]
            self.assertEqual(params["orderby"], "TransactionDate DESC")
            self.assertEqual(params["filters"], "AccountID,eq,9")
            self.assertEqual(int(params["pagesize"]), 2)
            self.assertEqual(int(params["pagenumber"]), 1)
            # Older row first. A Python sort of this slice would flip them.
            return FakeResponse(
                200,
                body=[
                    {"PaymentID": 1, "TransactionDate": "2019-01-01", "Amount": 5, "AccountID": 9},
                    {"PaymentID": 2, "TransactionDate": "2024-06-01", "Amount": 8, "AccountID": 9},
                ],
                headers={"X-Total-Results": "900"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_list_payments(9, limit=2))
        self.assertEqual([row["payment_id"] for row in raw["data"]], [1, 2])
        self.assertEqual([row["transaction_date"] for row in raw["data"]], ["2019-01-01", "2024-06-01"])
        self.assertTrue(raw["meta"]["truncated"])
        self.assertEqual(raw["meta"]["total"], 900)
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.auth_calls, 0)

    def test_explicit_orderby_wins_over_extra_params(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(kwargs["params"]["orderby"], "TransactionDate DESC")
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        client = self.make_client()
        self.transport.on_api = api
        client.get("/Payments", orderby="TransactionDate DESC", extra_params={"orderby": "Amount"})

    def _balance_transport(self, balance_body: Any, receivables: Any) -> list[str]:
        urls: list[str] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            urls.append(url)
            if url.endswith("/Balance"):
                self.assertNotIn("pagesize", kwargs.get("params") or {})
                return FakeResponse(200, body=balance_body)
            if url.endswith("/OpenReceivables"):
                return FakeResponse(200, body=receivables, headers={"X-Total-Results": "1"})
            if "/Tenants" in url:
                return FakeResponse(
                    200,
                    body=[{"TenantID": 42, "Name": "Ada"}],
                    headers={"X-Total-Results": "1"},
                )
            raise AssertionError(url)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        return urls

    def test_tenant_balance_uses_the_balance_endpoint_not_a_charge_sum(self) -> None:
        urls = self._balance_transport(
            {"Balance": 25},
            [{"ChargeID": 7, "Amount": 200, "AmountAllocated": 0, "TransactionDate": "2020-01-01"}],
        )
        raw = json.loads(self.server.rm_tenant_balance(42))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["data"]["balance"], 25)
        self.assertEqual(raw["data"]["balance_field"], "Balance")
        self.assertEqual(raw["data"]["open_charges"][0]["balance"], 200)
        self.assertNotEqual(raw["data"]["balance"], 200)
        self.assertIn("prepayments", raw["data"]["balance_note"])
        self.assertIn("OpenReceivables", raw["data"]["balance_note"])
        self.assertTrue(any(url.endswith("/Tenants/42/Balance") for url in urls))
        self.assertTrue(any(url.endswith("/Tenants/42/OpenReceivables") for url in urls))
        self.assertFalse(any("/Charges" in url for url in urls))
        self.assertEqual(self.transport.auth_calls, 0)

    def test_balance_field_names_are_read_defensively(self) -> None:
        cases = [
            (25.5, 25.5, "number"),
            ("10.25", 10.25, "number"),
            ({"balance": "10.5"}, 10.5, "balance"),
            ({"TenantBalance": "1,250.00"}, 1250.0, "TenantBalance"),
            ({"Unapplied": 12.5}, 12.5, "Unapplied"),
            ([{"Balance": 8}], 8.0, "Balance"),
            ({"TenantID": 9, "Name": "Ada"}, None, None),
            ({"Foo": "nope", "Bar": 1}, None, None),
        ]
        for body, expected, field in cases:
            with self.subTest(body=body):
                self.transport.calls.clear()
                self._balance_transport(
                    body,
                    [{"ChargeID": 1, "AmountDue": 40, "TransactionDate": "2021-06-01"}],
                )
                raw = json.loads(self.server.rm_tenant_balance(42, include_charges=False))
                self.assertEqual(raw["data"]["balance"], expected)
                self.assertEqual(raw["data"]["balance_field"], field)
                self.assertNotIn("open_charges", raw["data"])
                self.assertFalse(any(call["url"].endswith("/OpenReceivables") for call in self.transport.calls))
                if expected is None:
                    self.assertNotEqual(raw["data"]["balance"], 40)

    def test_open_receivables_amount_alias_and_wrapper(self) -> None:
        self._balance_transport(
            {"CurrentBalance": 0},
            {"OpenReceivables": [{"ID": 4, "AmountDue": 15, "TransactionDate": "2022-01-01"}]},
        )
        raw = json.loads(self.server.rm_tenant_balance(42))
        self.assertEqual(raw["data"]["balance"], 0)
        self.assertEqual(raw["data"]["balance_field"], "CurrentBalance")
        self.assertEqual(len(raw["data"]["open_charges"]), 1)
        self.assertEqual(raw["data"]["open_charges"][0]["amount"], 15)
        self.assertEqual(raw["data"]["open_charges"][0]["charge_id"], 4)

    def test_get_retries_gateway_errors_without_logging_in(self) -> None:
        log_lines: list[str] = []

        class Grab(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_lines.append(record.getMessage())

        handler = Grab()
        logger = logging.getLogger("rm-mcp.client")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        tries = {"n": 0}

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertEqual(_header_token(kwargs.get("headers")), TOKEN)
            tries["n"] += 1
            if tries["n"] < GET_TRANSIENT_RETRIES + 1:
                return FakeResponse(503, text="gateway down, token=should-not-matter")
            return FakeResponse(200, body=[])

        client = self.make_client()
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        self.transport.on_api = api
        try:
            response = client.request("GET", "/Properties")
        finally:
            logger.removeHandler(handler)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(tries["n"], GET_TRANSIENT_RETRIES + 1)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0])
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(json.loads(self.cache_path.read_text())["token"], TOKEN)
        logged = " ".join(log_lines)
        self.assertNotIn(PASSWORD, logged)
        self.assertNotIn(TOKEN, logged)
        self.assertNotIn("should-not-matter", logged)

    def test_retry_after_is_honored_and_capped(self) -> None:
        def api_once(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if len(self.transport.calls) == 1:
                return FakeResponse(429, text="slow down", headers={"retry-after": "2"})
            return FakeResponse(200, body=[])

        client = self.make_client()
        self.transport.on_api = api_once
        client.request("GET", "/Notes")
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(self.transport.auth_calls, 0)

        self.sleeps.clear()
        self.transport.calls.clear()

        def api_capped(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if len(self.transport.calls) == 1:
                return FakeResponse(504, text="later", headers={"Retry-After": "999999"})
            return FakeResponse(200, body=[])

        self.transport.on_api = api_capped
        client.request("GET", "/Notes")
        self.assertEqual(self.sleeps, [GET_RETRY_AFTER_CAP_SECONDS])

    def test_retry_after_http_date(self) -> None:
        from datetime import datetime, timezone
        from email.utils import formatdate

        class Frozen(datetime):
            @classmethod
            def now(cls, tz: timezone | None = None) -> datetime:
                return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        header = formatdate(datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp(), usegmt=True)

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if len(self.transport.calls) == 1:
                return FakeResponse(503, text="later", headers={"Retry-After": header})
            return FakeResponse(200, body=[])

        client = self.make_client()
        self.transport.on_api = api
        with mock.patch("rm_mcp.client.datetime", Frozen):
            client.request("GET", "/Notes")
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 5.0, places=3)

    def test_connection_timeout_is_retried_for_get_only(self) -> None:
        tries = {"n": 0}

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            tries["n"] += 1
            if tries["n"] < 3:
                raise requests.ConnectTimeout("connect timed out")
            return FakeResponse(200, body=[])

        client = self.make_client()
        self.transport.on_api = api
        response = client.request("GET", "/Units")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertEqual(self.transport.auth_calls, 0)

        self.sleeps.clear()
        self.transport.calls.clear()

        def always_timeout(method: str, url: str, **kwargs: Any) -> FakeResponse:
            raise requests.ConnectTimeout("connect timed out")

        self.transport.on_api = always_timeout
        with self.assertRaises(RMError) as caught:
            client.request("GET", "/Units")
        self.assertIn("timed out", str(caught.exception))
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(len(self.transport.calls), GET_TRANSIENT_RETRIES + 1)
        self.assertEqual(self.transport.auth_calls, 0)

    def test_post_and_delete_are_not_retried(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if method.upper() == "POST":
                return FakeResponse(503, text="nope")
            return FakeResponse(429, text="nope", headers={"Retry-After": "1"})

        client = self.make_client()
        self.transport.on_api = api
        with self.assertRaises(RMError) as post_error:
            client.request("POST", "/Bills", json_body={"Reference": "x"})
        self.assertEqual(post_error.exception.status, 503)
        with self.assertRaises(RMError) as delete_error:
            client.request("DELETE", "/Bills", params={"ids": "1"})
        self.assertEqual(delete_error.exception.status, 429)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual([call["method"] for call in self.transport.calls], ["POST", "DELETE"])

        self.transport.calls.clear()

        def post_timeout(method: str, url: str, **kwargs: Any) -> FakeResponse:
            raise requests.ConnectTimeout("connect timed out")

        self.transport.on_api = post_timeout
        with self.assertRaises(requests.ConnectTimeout):
            client.request("POST", "/Checks", json_body={"Amount": 1})
        self.assertEqual(len(self.transport.calls), 1)

    def test_gateway_retries_do_not_cause_a_second_login(self) -> None:
        """A 503 is retried in place. Only a real auth failure may log in, and only once."""

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            token = _header_token(kwargs.get("headers"))
            if token == "old-token":
                if sum(1 for call in self.transport.calls if "old-token" == _header_token(call["kwargs"].get("headers"))) == 1:
                    return FakeResponse(503, text="busy")
                return FakeResponse(401, text="expired token")
            return FakeResponse(200, body=[])

        def auth(url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(kwargs["json"]["Password"], PASSWORD)
            self.assertNotIn("apitoken", json.dumps({k: v for k, v in kwargs.items() if k != "json"}))
            return FakeResponse(200, body="new-token")

        client = self.make_client(token="old-token")
        self.transport.on_api = api
        self.transport.on_auth = auth
        response = client.request("GET", "/Properties")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(json.loads(self.cache_path.read_text())["token"], "new-token")
        self.assertNotIn(PASSWORD, self.cache_path.read_text())

    def test_server_error_500_is_not_retried(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(500, text="boom")

        client = self.make_client()
        self.transport.on_api = api
        with self.assertRaises(RMError) as caught:
            client.request("GET", "/Properties")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.transport.auth_calls, 0)

    def test_dry_run_bill_still_does_not_post_and_its_lookup_filter_is_safe(self) -> None:
        audit = Path(self.tmp.name) / "audit.jsonl"

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertIn("/Bills/Search", url)
            return FakeResponse(200, body=[])

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        with mock.patch("rm_mcp.writes.AUDIT_PATH", audit):
            raw = json.loads(
                self.server.rm_create_vendor_bill(
                    account_id=8,
                    property_id=1,
                    gl_account_id=2,
                    amount=10,
                    transaction_date="2024-01-01",
                    post_date="2024-01-01",
                    due_date="2024-01-15;Amount,eq,0",
                    reference="INV;Amount,eq,0",
                )
            )
        self.assertTrue(raw["ok"])
        self.assertTrue(raw["data"]["dry_run"])
        self.assertIsNone(raw["data"]["sent"])
        self.assertEqual([call["method"] for call in self.transport.calls], ["GET"])
        expression = self.transport.calls[0]["kwargs"]["params"]["filterExpression"]
        self.assertEqual(
            expression,
            "AccountID,eq,8;Reference,eq,INVAmounteq0;DueDate,eq,2024-01-15",
        )
        self.assertNotIn(PASSWORD, json.dumps(raw))


if __name__ == "__main__":
    unittest.main()
