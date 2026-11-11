"""Offline write-guardrail tests. The HTTP layer is mocked; nothing may call Rent Manager."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import rm_mcp.server as server_module
import rm_mcp.writes as writes
from rm_mcp.client import RMClient

PASSWORD = "test-password"
TOKEN = "cached-token"
BASE = "https://rm.example.test"
_CREDS = {
    "base_url": BASE,
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
    "RM_WRITE_AUDIT_LOG",
    "username",
    "password",
    "base_url",
    "partner_token",
)

WRITE_TOOLS = (
    "rm_add_note",
    "rm_update_unit_udf",
    "rm_create_work_order",
    "rm_update_work_order",
    "rm_link_work_order",
    "rm_create_task",
    "rm_upload_unit_image",
    "rm_upload_history_attachment",
    "rm_attach_vendor_file",
    "rm_attach_work_order_photo",
    "rm_create_vendor_bill",
    "rm_attach_bill_file",
    "rm_update_vendor_bill",
    "rm_create_credit_card_transaction",
    "rm_attach_credit_card_file",
    "rm_update_credit_card_transaction",
    "rm_create_check",
    "rm_attach_check_file",
    "rm_update_check",
)

ISSUE = {
    "ServiceManagerIssueID": 42,
    "Title": "Old title",
    "Description": "desc",
    "Resolution": "",
    "CustomerDescription": "",
    "NoteText": "",
    "StatusID": 1,
    "AssignedToUserID": 4,
    "IsClosed": True,
    "CloseDate": "2026-01-02",
    "CloseUserID": 9,
}


def _reject_live(url: str) -> None:
    if not str(url).startswith(BASE):
        raise AssertionError(f"refusing non-test URL: {url}")


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, *, text: str | None = None):
        if text is None:
            raw = "" if body is None else json.dumps(body)
        else:
            raw = text
        self.status_code = status_code
        self.text = raw
        self.headers: dict[str, str] = {}
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


def _route_get(url: str) -> FakeResponse:
    if "/ServiceManagerIssues/" in url:
        return FakeResponse(200, dict(ISSUE))
    if "/UserDefinedValues" in url:
        return FakeResponse(200, [])
    if "/Units/" in url:
        return FakeResponse(200, {"UnitID": 7, "Name": "A"})
    if "/Bills/" in url:
        return FakeResponse(200, {"ID": 9, "AccountID": 8})
    if "/CreditCardTransactions/" in url:
        return FakeResponse(200, {"ID": 11})
    if "/Checks/" in url:
        return FakeResponse(200, {"ID": 13})
    return FakeResponse(200, [])


def _route_post(url: str, kwargs: dict[str, Any]) -> FakeResponse:
    if any(part in url for part in ("UploadAttachment", "UploadImage", "FileAttachments", "/Attachments")):
        return FakeResponse(200, {"FileID": 55, "HistoryID": 100})
    body = kwargs.get("json")
    if url.endswith("/HistoryNotes") or url.endswith("/History") or url.endswith("/Notes"):
        return FakeResponse(200, {"HistoryID": 100, "NoteID": 5})
    if "Link" in url:
        return FakeResponse(200, {"ServiceManagerIssueID": 42})
    if url.endswith("/ServiceManagerIssues"):
        issue_id = 77
        if isinstance(body, dict) and body.get("ServiceManagerIssueID") is not None:
            issue_id = body["ServiceManagerIssueID"]
        return FakeResponse(200, {"ServiceManagerIssueID": issue_id})
    if url.endswith("/Tasks"):
        return FakeResponse(200, {"TaskID": 3})
    if "UserDefinedValues" in url:
        return FakeResponse(200, {"UserDefinedValueID": 8, "UserDefinedFieldID": 710})
    if url.endswith("/Bills"):
        return FakeResponse(200, [{"ID": 9}])
    if url.endswith("/CreditCardTransactions"):
        return FakeResponse(200, [{"ID": 11}])
    if url.endswith("/Checks"):
        return FakeResponse(200, [{"ID": 13}])
    return FakeResponse(200, {"ID": 1})


class WriteGuardrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.cache_path = Path(self.tmp.name) / "token.json"
        self.sample = Path(self.tmp.name) / "note.txt"
        self.sample.write_bytes(b"photo-bytes")
        self._env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._audit_patch = mock.patch("rm_mcp.writes.AUDIT_PATH", self.audit)
        self._token_patch = mock.patch("rm_mcp.client.TOKEN_CACHE_PATH", self.cache_path)
        self._audit_patch.start()
        self._token_patch.start()
        self.transport = Transport()
        self.server = server_module
        self._saved_client = server_module._client
        server_module._client = None
        self.make_client()

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if method.upper() == "GET":
                return _route_get(url)
            return _route_post(url, kwargs)

        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("login")
        )

    def tearDown(self) -> None:
        server_module._client = self._saved_client
        self._token_patch.stop()
        self._audit_patch.stop()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def make_client(self) -> RMClient:
        client = RMClient(dict(_CREDS))
        self.transport.install(client)
        self.cache_path.write_text(json.dumps({"token": TOKEN, "ts": 1}))
        client._token = TOKEN
        server_module._client = client
        return client

    def write_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.transport.calls if call["method"] != "GET"]

    def clear_calls(self) -> None:
        self.transport.calls.clear()

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines() if line.strip()]

    def events_for(self, key: str) -> list[dict[str, Any]]:
        return [event for event in self.audit_events() if event.get("idempotency_key") == key]

    def path_of(self, url: str) -> str:
        self.assertTrue(url.startswith(BASE), url)
        return url[len(BASE) :]

    def header(self, kwargs: dict[str, Any], name: str) -> str | None:
        for key, value in (kwargs.get("headers") or {}).items():
            if str(key).lower() == name.lower():
                return str(value)
        return None

    def cases(self) -> list[dict[str, Any]]:
        sample = str(self.sample)
        bill = [{
            "AccountID": 8,
            "AccountType": "Vendor",
            "TransactionDate": "2024-01-01",
            "PostDate": "2024-01-01",
            "DueDate": "2024-01-15",
            "BillDetails": [{
                "PropertyID": 1,
                "GLAccountID": 2,
                "Amount": 10.0,
                "Comment": "parts",
            }],
        }]
        card = [{
            "CreditCardID": 2,
            "AccountID": 8,
            "AccountType": "Vendor",
            "TransactionDate": "2024-02-01",
            "CreditCardTransactionDetails": [{
                "PropertyID": 1,
                "GLAccountID": 2,
                "Amount": 12.5,
            }],
        }]
        check = [{
            "BankID": 6,
            "AccountID": 8,
            "AccountType": "Vendor",
            "TransactionDate": "2024-03-01",
            "CheckDetails": [{
                "PropertyID": 1,
                "GLAccountID": 2,
                "Amount": 20.0,
            }],
            "Payee": "Ace Plumbing",
        }]
        updated_issue = {
            "ServiceManagerIssueID": 42,
            "Title": "New title",
            "Description": "desc",
            "Resolution": "",
            "CustomerDescription": "",
            "NoteText": "",
            "StatusID": 1,
            "AssignedToUserID": 4,
        }
        return [
            {
                "name": "rm_add_note",
                "call": lambda kw: self.server.rm_add_note(
                    entity_type="Tenant", parent_id=5, note="hello", **kw
                ),
                "steps": [{
                    "path": "/Tenants/5/HistoryNotes",
                    "json": {
                        "HistoryType": "Note",
                        "ParentID": 5,
                        "EntityType": "Tenant",
                        "Note": "hello",
                    },
                }],
            },
            {
                "name": "rm_update_unit_udf",
                "call": lambda kw: self.server.rm_update_unit_udf(
                    unit_id=7, field_id=710, value="yes", **kw
                ),
                "steps": [{
                    "path": "/Units/7/UserDefinedValues",
                    "json": {"UserDefinedFieldID": 710, "Value": "Yes"},
                }],
            },
            {
                "name": "rm_create_work_order",
                "call": lambda kw: self.server.rm_create_work_order(
                    title="Leaky faucet", description="drip", **kw
                ),
                "steps": [{
                    "path": "/ServiceManagerIssues",
                    "json": {"Title": "Leaky faucet", "Description": "drip"},
                }],
            },
            {
                "name": "rm_update_work_order",
                "call": lambda kw: self.server.rm_update_work_order(
                    work_order_id=42, title="New title", **kw
                ),
                "steps": [{
                    "path": "/ServiceManagerIssues",
                    "json": updated_issue,
                    "absent": ["IsClosed", "CloseDate", "CloseUserID"],
                }],
            },
            {
                "name": "rm_link_work_order",
                "call": lambda kw: self.server.rm_link_work_order(
                    work_order_id=42, unit_id=9, **kw
                ),
                "steps": [{
                    "path": "/ServiceManagerIssues/42/LinkUnits",
                    "json": [9],
                }],
            },
            {
                "name": "rm_create_task",
                "call": lambda kw: self.server.rm_create_task(subject="Call vendor", **kw),
                "steps": [{
                    "path": "/Tasks",
                    "json": {"Subject": "Call vendor", "Description": ""},
                }],
            },
            {
                "name": "rm_upload_unit_image",
                "call": lambda kw: self.server.rm_upload_unit_image(
                    unit_id=7, file_path=sample, **kw
                ),
                "steps": [{
                    "path": "/Units/7/UploadImage",
                    "data": {
                        "Payload": '[{"File": {"MetaTag": "Image1"}, "ImageTypeID": 1}]',
                    },
                    "file_field": "Image1",
                }],
            },
            {
                "name": "rm_upload_history_attachment",
                "call": lambda kw: self.server.rm_upload_history_attachment(
                    history_id=15, file_path=sample, **kw
                ),
                "steps": [{
                    "path": "/HistoryNotes/15/UploadAttachment",
                    "data": {"File": '{"HistoryID": 15}'},
                    "file_field": "Attachment",
                }],
            },
            {
                "name": "rm_attach_vendor_file",
                "call": lambda kw: self.server.rm_attach_vendor_file(
                    vendor_id=4, file_path=sample, note="w9", **kw
                ),
                "steps": [
                    {
                        "path": "/Vendors/4/HistoryNotes",
                        "json": {"Note": "w9"},
                    },
                    {
                        "path": "/HistoryNotes/100/UploadAttachment",
                        "data": {"File": '{"HistoryID": "100"}'},
                        "file_field": "Attachment",
                    },
                ],
            },
            {
                "name": "rm_attach_work_order_photo",
                "call": lambda kw: self.server.rm_attach_work_order_photo(
                    work_order_id=42, file_path=sample, note="pic", **kw
                ),
                "steps": [
                    {
                        "path": "/ServiceManagerIssues/42/History",
                        "json": {
                            "HistoryType": "Note",
                            "ParentID": 42,
                            "EntityType": "ServiceManagerIssue",
                            "Note": "pic",
                        },
                    },
                    {
                        "path": "/HistoryNotes/100/UploadAttachment",
                        "data": {"File": '{"HistoryID": "100"}'},
                        "file_field": "Attachment",
                    },
                ],
            },
            {
                "name": "rm_create_vendor_bill",
                "call": lambda kw: self.server.rm_create_vendor_bill(
                    account_id=8,
                    property_id=1,
                    gl_account_id=2,
                    amount=10,
                    transaction_date="2024-01-01",
                    post_date="2024-01-01",
                    due_date="2024-01-15",
                    detail_comment="parts",
                    **kw,
                ),
                "steps": [{
                    "path": "/Bills",
                    "json": bill,
                    "params": {"embeds": "BillDetails"},
                    "location": "1",
                }],
            },
            {
                "name": "rm_attach_bill_file",
                "call": lambda kw: self.server.rm_attach_bill_file(
                    bill_id=9, file_path=sample, **kw
                ),
                "steps": [{
                    "path": "/Bills/9/UploadAttachment",
                    "data": {},
                    "file_field": "file",
                    "location": "1",
                }],
            },
            {
                "name": "rm_update_vendor_bill",
                "call": lambda kw: self.server.rm_update_vendor_bill(
                    bill_id=9, fields_json='{"Comment": "fixed"}', **kw
                ),
                "steps": [{
                    "path": "/Bills",
                    "json": [{"ID": 9, "Comment": "fixed"}],
                    "params": {"fields": "ID,Comment"},
                    "location": "1",
                }],
            },
            {
                "name": "rm_create_credit_card_transaction",
                "call": lambda kw: self.server.rm_create_credit_card_transaction(
                    credit_card_id=2,
                    account_id=8,
                    property_id=1,
                    gl_account_id=2,
                    amount=12.5,
                    transaction_date="2024-02-01",
                    **kw,
                ),
                "steps": [{
                    "path": "/CreditCardTransactions",
                    "json": card,
                    "params": {"embeds": "CreditCardTransactionDetails"},
                    "location": "1",
                }],
            },
            {
                "name": "rm_attach_credit_card_file",
                "call": lambda kw: self.server.rm_attach_credit_card_file(
                    transaction_id=11, file_path=sample, description="scan", **kw
                ),
                "steps": [{
                    "path": "/CreditCardTransactions/11/Attachments",
                    "data": {"body": '[{"MetaTag": "file1", "Description": "scan"}]'},
                    "file_field": "file1",
                    "location": "1",
                }],
            },
            {
                "name": "rm_update_credit_card_transaction",
                "call": lambda kw: self.server.rm_update_credit_card_transaction(
                    transaction_id=11, fields_json='{"Comment": "memo"}', **kw
                ),
                "steps": [{
                    "path": "/CreditCardTransactions",
                    "json": [{"ID": 11, "Comment": "memo"}],
                    "params": {"fields": "ID,Comment"},
                    "location": "1",
                }],
            },
            {
                "name": "rm_create_check",
                "call": lambda kw: self.server.rm_create_check(
                    bank_id=6,
                    account_id=8,
                    property_id=1,
                    gl_account_id=2,
                    amount=20,
                    transaction_date="2024-03-01",
                    payee="Ace Plumbing",
                    **kw,
                ),
                "steps": [{
                    "path": "/Checks",
                    "json": check,
                    "params": {"embeds": "CheckDetails"},
                    "location": "1",
                }],
            },
            {
                "name": "rm_attach_check_file",
                "call": lambda kw: self.server.rm_attach_check_file(
                    check_id=13, file_path=sample, description="scan", **kw
                ),
                "steps": [{
                    "path": "/Checks/13/FileAttachments",
                    "data": {"body": '[{"MetaTag": "File1", "Description": "scan"}]'},
                    "file_field": "File1",
                    "location": "1",
                }],
            },
            {
                "name": "rm_update_check",
                "call": lambda kw: self.server.rm_update_check(
                    check_id=13, fields_json='{"Reference": "1001"}', **kw
                ),
                "steps": [{
                    "path": "/Checks",
                    "json": [{"ID": 13, "Reference": "1001"}],
                    "params": {"fields": "ID,Reference"},
                    "location": "1",
                }],
            },
        ]

    def assert_step(self, call: dict[str, Any], expected: dict[str, Any]) -> None:
        self.assertEqual(call["method"], "POST")
        self.assertEqual(self.path_of(call["url"]), expected["path"])
        self.assertNotIn("Unlink", expected["path"])
        self.assertNotIn("unlink", self.path_of(call["url"]))
        kwargs = call["kwargs"]
        if "json" in expected:
            self.assertEqual(kwargs.get("json"), expected["json"])
            for field in expected.get("absent") or []:
                self.assertNotIn(field, kwargs["json"])
        if "data" in expected:
            self.assertEqual(kwargs.get("data"), expected["data"])
        if "file_field" in expected:
            files = kwargs.get("files") or {}
            self.assertIn(expected["file_field"], files)
            filename, _handle, content_type = files[expected["file_field"]]
            self.assertEqual(filename, "note.txt")
            self.assertEqual(content_type, "text/plain")
        if "params" in expected:
            self.assertEqual(kwargs.get("params"), expected["params"])
        else:
            self.assertFalse(kwargs.get("params"))
        if expected.get("location"):
            self.assertEqual(self.header(kwargs, "x-rm12api-locationid"), expected["location"])
        if expected["path"] == "/CreditCardTransactions" and isinstance(kwargs.get("json"), list):
            self.assertNotIn("Amount", kwargs["json"][0])
        if expected["path"] == "/Checks" and isinstance(kwargs.get("json"), list):
            self.assertNotIn("Amount", kwargs["json"][0])
        if expected["path"] == "/Bills" and isinstance(kwargs.get("json"), list):
            self.assertNotIn("Amount", kwargs["json"][0])

    def test_tool_count_stays_at_76_and_write_tools_remain(self) -> None:
        names = [tool.name for tool in self.server.mcp._tool_manager.list_tools()]
        self.assertEqual(len(names), 76)
        self.assertEqual(len(names), len(set(names)))
        for name in WRITE_TOOLS:
            self.assertIn(name, names)
        for name in names:
            lowered = name.lower()
            self.assertNotIn("delete", lowered)
            self.assertNotIn("unlink", lowered)
            self.assertNotIn("close", lowered)

    def test_each_write_tool_dry_run_approval_send_audit_and_idempotency(self) -> None:
        self.assertEqual(len(self.cases()), len(WRITE_TOOLS))
        for case in self.cases():
            name = case["name"]
            with self.subTest(name=name, phase="dry_run"):
                self.clear_calls()
                raw = json.loads(case["call"]({
                    "dry_run": True,
                    "idempotency_key": f"dry-{name}",
                    "approved_by": "steve",
                }))
                self.assertTrue(raw["ok"], raw)
                self.assertTrue(raw["data"]["dry_run"])
                self.assertIsNone(raw["data"]["sent"])
                self.assertEqual(self.write_calls(), [])
                self.assertEqual(self.transport.auth_calls, 0)
                dry_events = self.events_for(f"dry-{name}")
                self.assertTrue(any(event["result"] == "dry_run" for event in dry_events))
                self.assertTrue(all(not event.get("idempotency_enforced") for event in dry_events))
                self.assertFalse(any(event["result"] in {"pending", "committed"} for event in dry_events))

            with self.subTest(name=name, phase="approval"):
                self.clear_calls()
                raw = json.loads(case["call"]({
                    "dry_run": False,
                    "idempotency_key": f"noapprove-{name}",
                    "approved_by": "",
                }))
                self.assertFalse(raw["ok"], raw)
                self.assertEqual(raw["error"]["code"], "approval_required")
                self.assertEqual(self.write_calls(), [])
                rejected = self.events_for(f"noapprove-{name}")
                self.assertTrue(any(
                    event["result"] == "rejected_missing_approval" for event in rejected
                ))
                self.assertFalse(any(event["result"] == "pending" for event in rejected))

            with self.subTest(name=name, phase="dry_run_does_not_consume_key"):
                self.clear_calls()
                raw = json.loads(case["call"]({
                    "dry_run": True,
                    "idempotency_key": f"live-{name}",
                    "approved_by": "steve",
                }))
                self.assertTrue(raw["ok"], raw)
                self.assertEqual(self.write_calls(), [])

            with self.subTest(name=name, phase="approved"):
                self.clear_calls()
                raw = json.loads(case["call"]({
                    "dry_run": False,
                    "idempotency_key": f"live-{name}",
                    "approved_by": "steve",
                }))
                self.assertTrue(raw["ok"], raw)
                self.assertFalse(raw["data"].get("idempotent_replay"))
                calls = self.write_calls()
                self.assertEqual(len(calls), len(case["steps"]), calls)
                for call, expected in zip(calls, case["steps"]):
                    self.assert_step(call, expected)
                live_events = self.events_for(f"live-{name}")
                self.assertTrue(any(event["result"] == "sent" for event in live_events))
                self.assertEqual(
                    sum(1 for event in live_events if event["result"] == "committed"),
                    1,
                )
                self.assertNotIn(PASSWORD, self.audit.read_text())
                self.assertNotIn(TOKEN, self.audit.read_text())
                first = raw

            with self.subTest(name=name, phase="replay"):
                before = len(self.write_calls())
                raw = json.loads(case["call"]({
                    "dry_run": False,
                    "idempotency_key": f"live-{name}",
                    "approved_by": "steve",
                }))
                self.assertTrue(raw["ok"], raw)
                self.assertTrue(raw["data"]["idempotent_replay"])
                self.assertEqual(raw["data"]["entity_ids"], first["data"]["entity_ids"])
                self.assertEqual(raw["data"]["original_audit_id"], first["data"]["audit_id"])
                self.assertEqual(len(self.write_calls()), before)
                live_events = self.events_for(f"live-{name}")
                self.assertEqual(
                    sum(1 for event in live_events if event["result"] == "committed"),
                    1,
                )
                self.assertTrue(any(event["result"] == "idempotent_replay" for event in live_events))

    def test_dry_run_is_the_default_and_blank_approval_does_not_send(self) -> None:
        self.clear_calls()
        raw = json.loads(self.server.rm_create_work_order(title="Leaky faucet"))
        self.assertTrue(raw["ok"], raw)
        self.assertTrue(raw["data"]["dry_run"])
        self.assertEqual(self.write_calls(), [])

        self.clear_calls()
        raw = json.loads(self.server.rm_create_work_order(
            title="Leaky faucet", dry_run=False, approved_by="   ", idempotency_key="blank-approval"
        ))
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"]["code"], "approval_required")
        self.assertEqual(self.write_calls(), [])

    def test_udf_allowlist_and_yes_no_normalization(self) -> None:
        for field_id in (0, 1, 709, 711, 733, 999):
            with self.subTest(field_id=field_id):
                self.clear_calls()
                raw = json.loads(self.server.rm_update_unit_udf(
                    unit_id=7,
                    field_id=field_id,
                    value="Yes",
                    dry_run=False,
                    approved_by="steve",
                    idempotency_key=f"bad-udf-{field_id}",
                ))
                self.assertFalse(raw["ok"], raw)
                self.assertIn("not allowlisted", raw["error"]["message"])
                self.assertEqual(self.write_calls(), [])

        self.clear_calls()
        raw = json.loads(self.server.rm_update_unit_udf(
            unit_id=7, field_id=731, value="true", dry_run=False,
            approved_by="steve", idempotency_key="udf-731",
        ))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.write_calls()[0]["kwargs"]["json"], {
            "UserDefinedFieldID": 731,
            "Value": "Yes",
        })

        self.clear_calls()
        raw = json.loads(self.server.rm_update_unit_udf(
            unit_id=7, field_id=732, value="1500", dry_run=False,
            approved_by="steve", idempotency_key="udf-732",
        ))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.write_calls()[0]["kwargs"]["json"], {
            "UserDefinedFieldID": 732,
            "Value": "1500",
        })

        blocked = writes.policy_block_reason([{
            "method": "POST",
            "path": "/Units/7/UserDefinedValues",
            "json_body": {"UserDefinedFieldID": 709, "Value": "Yes"},
        }])
        self.assertIn("not allowlisted", blocked or "")
        self.assertIsNone(writes.policy_block_reason([{
            "method": "POST",
            "path": "/Units/7/UserDefinedValues",
            "json_body": {"UserDefinedFieldID": 710, "Value": "Yes"},
        }]))

    def test_work_order_close_and_unlink_and_delete_stay_blocked(self) -> None:
        params = inspect.signature(self.server.rm_update_work_order).parameters
        self.assertFalse(any("close" in name.lower() for name in params))
        plan = writes.build_update_work_order_plan(
            work_order_id=42,
            current=dict(ISSUE),
            title="New title",
        )
        body = plan[0]["json_body"]
        self.assertNotIn("IsClosed", body)
        self.assertNotIn("CloseDate", body)
        self.assertNotIn("CloseUserID", body)
        self.assertEqual(body["Title"], "New title")
        self.assertEqual(body["ServiceManagerIssueID"], 42)

        client = server_module._client
        assert client is not None
        for label, request_plan in (
            ("delete", [{"method": "DELETE", "path": "/Bills/9"}]),
            ("unlink", [{"method": "POST", "path": "/ServiceManagerIssues/42/UnlinkUnits", "json_body": [9]}]),
            ("close", [{
                "method": "POST",
                "path": "/ServiceManagerIssues",
                "json_body": {"ServiceManagerIssueID": 42, "Title": "x", "IsClosed": True},
            }]),
        ):
            with self.subTest(label=label):
                self.clear_calls()
                result = writes.guarded_write(
                    client,
                    action="policy_probe",
                    dry_run=True,
                    idempotency_key=f"policy-{label}",
                    reason="probe",
                    approved_by="steve",
                    request_plan=request_plan,
                    idempotency_enforced=True,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "rejected_policy")
                self.assertEqual(self.write_calls(), [])
                self.assertTrue(any(
                    event["result"] == "rejected_policy"
                    for event in self.events_for(f"policy-{label}")
                ))

    def test_same_title_without_a_key_is_not_treated_as_a_duplicate(self) -> None:
        self.clear_calls()
        first = json.loads(self.server.rm_create_work_order(
            title="Leaky faucet", dry_run=False, approved_by="steve"
        ))
        second = json.loads(self.server.rm_create_work_order(
            title="Leaky faucet", dry_run=False, approved_by="steve"
        ))
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertFalse(first["data"].get("idempotent_replay"))
        self.assertFalse(second["data"].get("idempotent_replay"))
        self.assertEqual(len(self.write_calls()), 2)

    def test_failed_live_write_can_be_retried_with_the_same_key(self) -> None:
        attempts = {"n": 0}

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if method.upper() != "POST":
                return _route_get(url)
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResponse(500, text="nope")
            return FakeResponse(200, {"HistoryID": 5})

        self.transport.on_api = api
        self.clear_calls()
        first = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="hello",
            dry_run=False, approved_by="steve", idempotency_key="retry-me",
        ))
        self.assertFalse(first["ok"], first)
        second = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="hello",
            dry_run=False, approved_by="steve", idempotency_key="retry-me",
        ))
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["data"].get("idempotent_replay"))
        self.assertEqual(len(self.write_calls()), 2)
        self.assertTrue(any(event["result"] == "failed" for event in self.events_for("retry-me")))
        self.assertTrue(any(event["result"] == "committed" for event in self.events_for("retry-me")))

    def test_partial_send_consumes_the_key(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if method.upper() == "GET":
                return _route_get(url)
            if "UploadAttachment" in url:
                return FakeResponse(500, text="attach failed")
            return FakeResponse(200, {"HistoryID": 100})

        self.transport.on_api = api
        self.clear_calls()
        kwargs = {
            "dry_run": False,
            "approved_by": "steve",
            "idempotency_key": "partial-photo",
        }
        first = json.loads(self.server.rm_attach_work_order_photo(
            work_order_id=42, file_path=str(self.sample), note="pic", **kwargs
        ))
        self.assertFalse(first["ok"], first)
        self.assertEqual(len(self.write_calls()), 2)
        self.assertTrue(any(event["result"] == "partial" for event in self.events_for("partial-photo")))
        before = len(self.write_calls())
        second = json.loads(self.server.rm_attach_work_order_photo(
            work_order_id=42, file_path=str(self.sample), note="pic", **kwargs
        ))
        self.assertFalse(second["ok"], second)
        self.assertTrue(second["data"]["idempotent_replay"])
        self.assertEqual(second["data"]["entity_ids"].get("HistoryID"), 100)
        self.assertEqual(len(self.write_calls()), before)

    def test_same_key_with_a_different_body_conflicts(self) -> None:
        self.clear_calls()
        first = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="hello",
            dry_run=False, approved_by="steve", idempotency_key="once",
        ))
        self.assertTrue(first["ok"], first)
        second = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="goodbye",
            dry_run=False, approved_by="steve", idempotency_key="once",
        ))
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["error"]["code"], "idempotency_conflict")
        self.assertEqual(len(self.write_calls()), 1)

    def test_pending_record_without_an_outcome_blocks_a_resend(self) -> None:
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.audit.write_text(json.dumps({
            "idempotency_key": "stuck-key",
            "idempotency_enforced": True,
            "result": "pending",
            "action": "add_note",
            "fingerprint": "unknown",
        }) + "\n")
        self.clear_calls()
        raw = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="hello",
            dry_run=False, approved_by="steve", idempotency_key="stuck-key",
        ))
        self.assertFalse(raw["ok"], raw)
        self.assertEqual(raw["error"]["code"], "idempotency_uncertain")
        self.assertEqual(self.write_calls(), [])

    def test_idempotency_decision_reads_the_log_only(self) -> None:
        key = "k"
        fingerprint = "abc"
        other = "def"

        def decide(events: list[dict[str, Any]]) -> str:
            kind, _event = writes._idempotency_decision(events, key, fingerprint)
            return kind

        self.assertEqual(decide([]), "free")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": False, "result": "dry_run"},
            {"idempotency_key": key, "idempotency_enforced": False, "result": "committed", "fingerprint": fingerprint},
        ]), "free")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": True, "result": "pending"},
        ]), "uncertain")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": True, "result": "pending"},
            {"idempotency_key": key, "idempotency_enforced": True, "result": "attempt_failed"},
        ]), "uncertain")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": True, "result": "pending"},
            {"idempotency_key": key, "idempotency_enforced": True, "result": "failed"},
        ]), "free")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": True, "result": "pending"},
            {"idempotency_key": key, "idempotency_enforced": True, "result": "sent"},
            {
                "idempotency_key": key,
                "idempotency_enforced": True,
                "result": "committed",
                "fingerprint": fingerprint,
                "prior_result": {"ok": True},
            },
        ]), "replay")
        self.assertEqual(decide([
            {
                "idempotency_key": key,
                "idempotency_enforced": True,
                "result": "committed",
                "fingerprint": other,
            },
        ]), "conflict")
        self.assertEqual(decide([
            {"idempotency_key": key, "idempotency_enforced": True, "result": "pending"},
            {
                "idempotency_key": key,
                "idempotency_enforced": True,
                "result": "partial",
                "fingerprint": fingerprint,
            },
        ]), "replay")

    def test_audit_log_failure_blocks_before_send_and_approval_stays_visible(self) -> None:
        blocker = Path(self.tmp.name) / "not-a-directory"
        blocker.write_text("x")
        bad = blocker / "audit.jsonl"
        with mock.patch("rm_mcp.writes.AUDIT_PATH", bad):
            self.clear_calls()
            dry = json.loads(self.server.rm_add_note(
                entity_type="Tenant", parent_id=5, note="hello", dry_run=True
            ))
            self.assertFalse(dry["ok"], dry)
            self.assertEqual(dry["error"]["code"], "audit_log_failed")
            self.assertTrue(dry["data"]["intended_dry_run"])
            self.assertIsNone(dry["data"]["sent"])
            self.assertEqual(self.write_calls(), [])

            self.clear_calls()
            live = json.loads(self.server.rm_add_note(
                entity_type="Tenant", parent_id=5, note="hello",
                dry_run=False, approved_by="steve", idempotency_key="blocked-live",
            ))
            self.assertFalse(live["ok"], live)
            self.assertEqual(live["error"]["code"], "audit_log_failed")
            self.assertIn("Refusing the write", live["error"]["message"])
            self.assertIsNone(live["data"]["sent"])
            self.assertEqual(self.write_calls(), [])

            self.clear_calls()
            missing = json.loads(self.server.rm_add_note(
                entity_type="Tenant", parent_id=5, note="hello", dry_run=False
            ))
            self.assertFalse(missing["ok"], missing)
            self.assertEqual(missing["error"]["code"], "approval_required")
            self.assertIn("audit_error", missing["error"])
            self.assertEqual(self.write_calls(), [])

    def test_post_send_audit_failure_is_visible_and_blocks_a_retry(self) -> None:
        real = writes._append_event

        def flaky(path: Path, event: dict[str, Any]) -> str:
            if event.get("result") == "committed":
                raise writes.AuditLogError("disk full")
            return real(path, event)

        with mock.patch("rm_mcp.writes._append_event", flaky):
            self.clear_calls()
            raw = json.loads(self.server.rm_add_note(
                entity_type="Tenant", parent_id=5, note="hello",
                dry_run=False, approved_by="steve", idempotency_key="disk-full",
            ))
        self.assertFalse(raw["ok"], raw)
        self.assertEqual(raw["error"]["code"], "audit_log_failed")
        self.assertIn("Do not retry", raw["error"]["message"])
        self.assertIsNotNone(raw["data"]["sent"])
        self.assertEqual(len(self.write_calls()), 1)

        before = len(self.write_calls())
        again = json.loads(self.server.rm_add_note(
            entity_type="Tenant", parent_id=5, note="hello",
            dry_run=False, approved_by="steve", idempotency_key="disk-full",
        ))
        self.assertFalse(again["ok"], again)
        self.assertEqual(again["error"]["code"], "idempotency_uncertain")
        self.assertEqual(len(self.write_calls()), before)

    def test_audit_path_is_durable_configurable_and_redacts_secrets(self) -> None:
        self.assertEqual(
            writes.default_audit_path(),
            Path.home() / ".local" / "share" / "rm-mcp" / "write_audit.jsonl",
        )
        self.assertNotEqual(writes.default_audit_path(), Path("/tmp/rm_mcp_write_audit.jsonl"))
        self.assertEqual(writes.resolve_audit_path(), self.audit)

        custom = Path(self.tmp.name) / "durable" / "write_audit.jsonl"
        note = "env-override-note-9f3a"
        with mock.patch.dict(os.environ, {"RM_WRITE_AUDIT_LOG": str(custom)}):
            self.assertEqual(writes.resolve_audit_path(), custom)
            raw = json.loads(self.server.rm_add_note(
                entity_type="Tenant", parent_id=5, note=note, dry_run=True
            ))
        self.assertTrue(raw["ok"], raw)
        self.assertTrue(custom.is_file())
        self.assertEqual(custom.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(custom.stat().st_mode & 0o777, 0o600)
        self.assertIn(note, custom.read_text())
        if self.audit.exists():
            self.assertNotIn(note, self.audit.read_text())
        tmp_log = Path("/tmp/rm_mcp_write_audit.jsonl")
        if tmp_log.exists():
            self.assertNotIn(note, tmp_log.read_text())

        secret = "s3cret-password"
        visible = "visible-note-text"
        with mock.patch.dict(os.environ, {"RM_PASSWORD": secret}):
            raw = json.loads(self.server.rm_add_note(
                entity_type="Tenant",
                parent_id=5,
                note=f"reset {secret} please",
                dry_run=True,
                idempotency_key="secret-note",
            ))
            self.assertTrue(raw["ok"], raw)
            client = server_module._client
            assert client is not None
            probed = writes.guarded_write(
                client,
                action="secret_probe",
                dry_run=True,
                idempotency_key="secret-fields",
                reason="header x-rm12api-apitoken: abcdefghijklmnop",
                approved_by="steve",
                request_plan=[{
                    "method": "POST",
                    "path": "/Notes",
                    "json_body": {
                        "Name": visible,
                        "Password": "hunter2-secret",
                        "api_token": "sekrit-token",
                    },
                }],
                meta={"password": "hunter2-secret"},
            )
            self.assertTrue(probed["ok"], probed)
        logged = self.audit.read_text()
        self.assertIn(visible, logged)
        self.assertNotIn(secret, logged)
        self.assertNotIn("hunter2-secret", logged)
        self.assertNotIn("sekrit-token", logged)
        self.assertNotIn("abcdefghijklmnop", logged)
        self.assertIn("[redacted]", logged)
        self.assertNotIn(PASSWORD, logged)
        self.assertNotIn(TOKEN, logged)


if __name__ == "__main__":
    unittest.main()
