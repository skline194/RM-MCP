"""Read-only people, owner, history, and rent-roll tools. HTTP is mocked."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from test_data_correctness import (
    TOKEN,
    FakeResponse,
    Transport,
    _CREDS,
    _ENV_KEYS,
    _header_token,
)

from rm_mcp.client import RMClient
from rm_mcp.server import (
    _TENANT_RENT_EMBEDS,
    _UNIT_RENT_EMBEDS,
    _assemble_rent_roll,
    _merge_contact_history,
)

PASSWORD = _CREDS["password"]


def _path(url: str) -> str:
    return url.split("rm.example.test", 1)[-1]


class EasyReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "token.json"
        self._env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._path_patch = unittest.mock.patch("rm_mcp.client.TOKEN_CACHE_PATH", self.cache_path)
        self._path_patch.start()
        self.transport = Transport()
        self._sleep_patch = unittest.mock.patch("rm_mcp.client.time.sleep", return_value=None)
        self._sleep_patch.start()
        import rm_mcp.server as server

        self.server = server
        self._saved_client = server._client
        server._client = None

    def tearDown(self) -> None:
        self.server._client = self._saved_client
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

    def test_people_search_kind_selection_and_plain_search_text(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertEqual(_header_token(kwargs.get("headers")), TOKEN)
            params = kwargs["params"]
            seen.append((_path(url), params))
            self.assertEqual(params["searchText"], "Smith, Jr.")
            self.assertNotIn("filters", params)
            kind_rows = {
                "/Tenants/QuickSearch": [
                    {
                        "TenantID": 3,
                        "Name": "Ada Smith",
                        "Status": "Current",
                        "PropertyID": 4,
                        "PhoneNumbers": [
                            {"PhoneNumber": "111", "IsPrimary": False},
                            {"PhoneNumber": "555", "IsPrimary": True},
                        ],
                    }
                ],
                "/Prospects/QuickSearch": [
                    {"ProspectID": 4, "Name": "Pat Smith", "ProspectStatus": "Customer", "PropertyID": 3}
                ],
                "/Owners/QuickSearch": [{"OwnerID": 5, "Name": "Oak Trust", "Status": "Current"}],
                "/Vendors/QuickSearch": [{"VendorID": 2, "Name": "Ace Plumbing", "IsActive": True}],
                "/Contacts/QuickSearch": [
                    {"Email": "ignored@example.com"},
                    {
                        "ContactID": 8,
                        "FirstName": "Ann",
                        "LastName": "Lee",
                        "Email": "ann@example.com",
                        "IsActive": False,
                        "ParentID": 7,
                        "ParentType": "Tenant",
                    },
                ],
            }
            rows = kind_rows[_path(url)]
            return FakeResponse(200, body=rows, headers={"X-Total-Results": str(len(rows))})

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_search_people("Smith, Jr."))

        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(
            [path for path, _params in seen],
            [
                "/Tenants/QuickSearch",
                "/Prospects/QuickSearch",
                "/Owners/QuickSearch",
                "/Vendors/QuickSearch",
                "/Contacts/QuickSearch",
            ],
        )
        by_kind = {hit["kind"]: hit for hit in raw["data"]}
        self.assertEqual(set(by_kind), {"tenant", "prospect", "owner", "vendor", "contact"})
        self.assertEqual(by_kind["tenant"]["phone"], "555")
        self.assertIsNone(by_kind["tenant"]["email"])
        self.assertEqual(by_kind["tenant"]["status"], "Current")
        self.assertEqual(by_kind["tenant"]["property_id"], 4)
        self.assertEqual(by_kind["prospect"]["status"], "Customer")
        self.assertEqual(by_kind["vendor"]["status"], "Active")
        self.assertEqual(by_kind["contact"]["name"], "Ann Lee")
        self.assertEqual(by_kind["contact"]["email"], "ann@example.com")
        self.assertEqual(by_kind["contact"]["status"], "Inactive")
        self.assertEqual(by_kind["contact"]["parent_id"], 7)
        self.assertEqual(by_kind["contact"]["parent_type"], "Tenant")
        self.assertEqual(raw["meta"]["counts"]["contact"], 1)
        self.assertFalse(raw["meta"]["truncated"])
        self.assertNotIn(PASSWORD, json.dumps(raw))

        self.transport.calls.clear()
        seen.clear()
        picked = json.loads(self.server.rm_search_people("Smith, Jr.", kinds="owner, owner, vendor"))
        self.assertTrue(picked["ok"], picked)
        self.assertEqual([path for path, _params in seen], ["/Owners/QuickSearch", "/Vendors/QuickSearch"])
        self.assertEqual(picked["meta"]["kinds"], ["owner", "vendor"])

        self.transport.calls.clear()
        rejected = json.loads(self.server.rm_search_people("Smith", kinds="landlord"))
        self.assertFalse(rejected["ok"])
        self.assertIn("people kind", rejected["error"]["message"])
        self.assertEqual(self.transport.calls, [])

        empty = json.loads(self.server.rm_search_people("   "))
        self.assertFalse(empty["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_people_search_reports_a_per_kind_cutoff(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(kwargs["params"]["pagesize"], 1)
            return FakeResponse(
                200,
                body=[{"TenantID": 1, "Name": "Ada", "Status": "Current"}],
                headers={"X-Total-Results": "5"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_search_people("Ada", kinds="tenants", limit=1))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(len(raw["data"]), 1)
        self.assertTrue(raw["meta"]["truncated"])
        self.assertEqual(raw["meta"]["totals_by_kind"]["tenant"], 5)
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertEqual(self.transport.auth_calls, 0)

    def test_people_search_throttle_and_auth_failure_do_not_multiply_logins(self) -> None:
        tries = {"n": 0}

        def throttled(method: str, url: str, **kwargs: Any) -> FakeResponse:
            tries["n"] += 1
            if tries["n"] == 1:
                return FakeResponse(503, text="gateway")
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        self.make_client()
        self.transport.on_api = throttled
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_search_people("Ada", kinds="tenant"))
        self.assertTrue(raw["ok"], raw)
        self.assertGreaterEqual(tries["n"], 2)
        self.assertEqual(self.transport.auth_calls, 0)

        def denied(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if _header_token(kwargs.get("headers")) == "old-token":
                return FakeResponse(401, text="expired token")
            if _path(url) == "/Tenants/QuickSearch":
                return FakeResponse(
                    200,
                    body=[{"TenantID": 3, "Name": "Ada", "Status": "Current"}],
                    headers={"X-Total-Results": "1"},
                )
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        def auth(url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(kwargs["json"]["Password"], PASSWORD)
            return FakeResponse(200, body="new-token")

        self.transport.calls.clear()
        self.make_client(token="old-token")
        self.transport.on_api = denied
        self.transport.on_auth = auth
        raw = json.loads(self.server.rm_search_people("Ada"))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.transport.auth_calls, 1)
        self.assertEqual(raw["data"][0]["id"], 3)
        self.assertEqual(raw["data"][0]["kind"], "tenant")
        gets = [call for call in self.transport.calls if call["method"] == "GET"]
        self.assertEqual(len(gets), 6)

    def test_get_all_sends_search_text_on_every_page(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(kwargs["params"]["searchText"], "ada")
            page = kwargs["params"]["pagenumber"]
            body = [{"TenantID": page}]
            return FakeResponse(200, body=body, headers={"X-Total-Results": "2"})

        client = self.make_client()
        self.transport.on_api = api
        rows = client.get_all(
            "/Tenants/QuickSearch",
            pagesize=1,
            max_pages=5,
            extra_params={"searchText": "ada"},
        )
        self.assertEqual([row["TenantID"] for row in rows], [1, 2])
        self.assertFalse(rows.truncated)
        self.assertEqual(self.transport.auth_calls, 0)

    def test_find_owner_sanitizes_filters_and_detail_reads_balance(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Owners" and params.get("filters", "").startswith("Name,ct,"):
                self.assertEqual(params["filters"], "Name,ct,OakIsActiveeqfalse")
                self.assertEqual(params["embeds"], "PhoneNumbers")
                self.assertNotIn(";", params["filters"])
                return FakeResponse(
                    200,
                    body=[
                        {
                            "OwnerID": 5,
                            "Name": "Oak Trust",
                            "DisplayName": "Oak",
                            "Status": "Current",
                            "EcheckEmail": "oak@example.com",
                            "PhoneNumbers": [
                                {"PhoneNumber": "111", "IsPrimary": False},
                                {"PhoneNumber": "222", "IsPrimary": True},
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Owners" and "OwnerID,eq,5" in params.get("filters", ""):
                self.assertEqual(params["embeds"], "Ownerships,Addresses,PhoneNumbers")
                return FakeResponse(
                    200,
                    body=[
                        {
                            "OwnerID": 5,
                            "Name": "Oak Trust",
                            "EcheckEmail": "oak@example.com",
                            "PhoneNumbers": [{"PhoneNumber": "222", "IsPrimary": True}],
                            "Addresses": [
                                {
                                    "AddressID": 1,
                                    "Street": "1 Main",
                                    "City": "Austin",
                                    "State": "TX",
                                    "PostalCode": "78701",
                                    "IsPrimary": True,
                                    "IsBilling": False,
                                }
                            ],
                            "Ownerships": [
                                {
                                    "OwnerContractID": 9,
                                    "OwnerID": 5,
                                    "PropertyID": 4,
                                    "Percentage": 100,
                                    "Reserve": 250,
                                    "ContractFrom": "2020-01-15T00:00:00",
                                    "ContractTo": None,
                                }
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Owners/5/Balance":
                self.assertIsNone(kwargs.get("params"))
                return FakeResponse(200, body=12.5)
            if path == "/Owners" and params.get("filters") is None and "filters" not in params:
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            raise AssertionError(f"unexpected {path} {params}")

        self.make_client()
        self.transport.on_api = api
        found = json.loads(self.server.rm_find_owner("Oak;IsActive,eq,false"))
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"][0]["phone"], "222")
        self.assertEqual(found["data"][0]["email"], "oak@example.com")
        self.assertEqual(found["data"][0]["status"], "Current")
        self.assertNotIn("balance", found["data"][0])
        self.assertEqual(self.transport.auth_calls, 0)

        detail = json.loads(self.server.rm_owner_detail(5))
        self.assertTrue(detail["ok"], detail)
        self.assertTrue(detail["meta"]["found"])
        self.assertEqual(detail["data"]["balance"], 12.5)
        self.assertEqual(detail["data"]["balance_field"], "number")
        self.assertEqual(detail["data"]["balance_source"], "GET /Owners/5/Balance")
        self.assertEqual(detail["data"]["ownerships"][0]["property_id"], 4)
        self.assertEqual(detail["data"]["ownerships"][0]["percentage"], 100.0)
        self.assertEqual(detail["data"]["ownerships"][0]["contract_from"], "2020-01-15")
        self.assertEqual(detail["data"]["addresses"][0]["city"], "Austin")
        self.assertTrue(detail["data"]["addresses"][0]["is_primary"])
        urls = [_path(call["url"]) for call in self.transport.calls]
        self.assertIn("/Owners/5/Balance", urls)

        self.transport.calls.clear()

        def missing(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(_path(url), "/Owners")
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        self.transport.on_api = missing
        gone = json.loads(self.server.rm_owner_detail(99))
        self.assertTrue(gone["ok"], gone)
        self.assertFalse(gone["meta"]["found"])
        self.assertIsNone(gone["data"])
        self.assertEqual([_path(call["url"]) for call in self.transport.calls], ["/Owners"])

        self.transport.calls.clear()
        self.transport.on_api = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called"))
        bad = json.loads(self.server.rm_find_owner(",,;"))
        self.assertFalse(bad["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_property_owners_reads_current_and_primary(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Properties/4/CurrentOwners":
                self.assertEqual(kwargs["params"]["embeds"], "PhoneNumbers")
                return FakeResponse(
                    200,
                    body=[
                        {"OwnerID": 6, "Name": "Other", "PhoneNumbers": [{"PhoneNumber": "333", "IsPrimary": True}]},
                        {"OwnerID": 5, "Name": "Oak Trust"},
                    ],
                    headers={"X-Total-Results": "2"},
                )
            if path == "/Properties/4/PrimaryOwner":
                self.assertEqual(kwargs["params"]["embeds"], "PhoneNumbers")
                return FakeResponse(200, body={"OwnerID": 5, "Name": "Oak Trust"})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_property_owners(4))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["data"]["primary_owner"]["owner_id"], 5)
        self.assertTrue(raw["data"]["primary_owner"]["is_primary"])
        flags = {row["owner_id"]: row["is_primary"] for row in raw["data"]["current_owners"]}
        self.assertEqual(flags, {6: False, 5: True})
        self.assertEqual(raw["data"]["current_owners"][0]["phone"], "333")
        self.assertTrue(raw["meta"]["primary_found"])
        self.assertEqual(
            [_path(call["url"]) for call in self.transport.calls],
            ["/Properties/4/CurrentOwners", "/Properties/4/PrimaryOwner"],
        )
        self.assertEqual(self.transport.auth_calls, 0)

        def missing_primary(method: str, url: str, **kwargs: Any) -> FakeResponse:
            if _path(url).endswith("/PrimaryOwner"):
                return FakeResponse(404, text="none")
            return FakeResponse(
                200,
                body=[{"OwnerID": 6, "Name": "Other"}],
                headers={"X-Total-Results": "1"},
            )

        self.transport.on_api = missing_primary
        raw = json.loads(self.server.rm_property_owners(4))
        self.assertTrue(raw["ok"], raw)
        self.assertIsNone(raw["data"]["primary_owner"])
        self.assertEqual(raw["data"]["current_owners"][0]["name"], "Other")
        self.assertFalse(raw["data"]["current_owners"][0]["is_primary"])
        self.assertFalse(raw["meta"]["primary_found"])
        self.assertEqual(self.transport.auth_calls, 0)

    def test_contact_history_merges_newest_first_and_leaves_list_history_alone(self) -> None:
        notes = [
            {"HistoryID": 1, "HistoryDate": "2024-01-01T00:00:00", "Note": "old note"},
            {"HistoryID": 5, "HistoryDate": "2024-06-01T09:00:00", "Note": "morning", "FileID": 9},
        ]
        calls = [
            {
                "HistoryID": 8,
                "HistoryDate": "2024-06-01T15:00:00",
                "Note": "afternoon call",
                "Result": "spoke",
                "IsContactMade": True,
            }
        ]
        emails = [{"HistoryID": 3, "HistoryDate": "2023-05-01T00:00:00", "Note": "email"}]
        visits = [{"HistoryID": 4, "Note": "undated visit"}]
        merged, capped = _merge_contact_history(
            {"visit": visits, "email": emails, "note": notes, "call": calls},
            limit=2,
            parent_id=9,
            entity_type="tenant",
        )
        self.assertTrue(capped)
        self.assertEqual([row["history_id"] for row in merged], [8, 5])
        self.assertEqual(merged[0]["type"], "call")
        self.assertEqual(merged[0]["entity_type"], "tenant")
        self.assertEqual(merged[0]["result"], "spoke")
        self.assertTrue(merged[1]["has_attachment"])

        tied, tied_capped = _merge_contact_history(
            {
                "note": [
                    {"HistoryID": 1, "HistoryDate": "2024-06-01T09:00:00", "Note": "a"},
                    {"HistoryID": 5, "HistoryDate": "2024-06-01T09:00:00", "Note": "b"},
                ]
            },
            limit=5,
            parent_id=1,
            entity_type="owner",
        )
        self.assertEqual([row["history_id"] for row in tied], [5, 1])
        self.assertFalse(tied_capped)

        undated, _ignored = _merge_contact_history(
            {
                "visit": [{"HistoryID": 4, "Note": "no date"}],
                "email": [{"HistoryID": 3, "HistoryDate": "2020-01-01", "Note": "old"}],
            },
            limit=5,
            parent_id=1,
            entity_type="prospect",
        )
        self.assertEqual([row["history_id"] for row in undated], [3, 4])
        self.assertEqual(undated[0]["parent_id"], 1)

        payloads = {
            "/Tenants/9/HistoryNotes": notes,
            "/Tenants/9/HistoryCalls": calls,
            "/Tenants/9/HistoryEmails": emails,
            "/Tenants/9/HistoryVisits": visits,
        }

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            self.assertNotIn("orderby", kwargs["params"])
            self.assertNotIn("filters", kwargs["params"])
            rows = payloads[path]
            return FakeResponse(200, body=rows, headers={"X-Total-Results": str(len(rows))})

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_contact_history("Tenant", 9, limit=10))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual([row["history_id"] for row in raw["data"]], [8, 5, 1, 3, 4])
        self.assertEqual([row["type"] for row in raw["data"]], ["call", "note", "note", "email", "visit"])
        self.assertFalse(raw["meta"]["truncated"])
        self.assertEqual(self.transport.auth_calls, 0)

        self.transport.calls.clear()

        def emails_only(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(_path(url), "/Prospects/4/HistoryEmails")
            return FakeResponse(200, body=emails, headers={"X-Total-Results": "1"})

        self.transport.on_api = emails_only
        raw = json.loads(self.server.rm_contact_history("prospect", 4, types="email"))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual([row["type"] for row in raw["data"]], ["email"])
        self.assertEqual([_path(call["url"]) for call in self.transport.calls], ["/Prospects/4/HistoryEmails"])

        self.transport.calls.clear()
        self.transport.on_api = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called"))
        rejected = json.loads(self.server.rm_contact_history("Unit", 9))
        self.assertFalse(rejected["ok"])
        self.assertIn("Tenant, Prospect, or Owner", rejected["error"]["message"])
        self.assertEqual(self.transport.calls, [])

        def combined(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(_path(url), "/Tenants/9/History")
            return FakeResponse(
                200,
                body=[{"HistoryID": 1, "HistoryType": "Note", "Note": "hi", "HistoryDate": "2024-01-02"}],
                headers={"X-Total-Results": "1"},
            )

        self.transport.on_api = combined
        legacy = json.loads(self.server.rm_list_history("Tenant", 9))
        self.assertTrue(legacy["ok"], legacy)
        self.assertEqual(legacy["data"][0]["history_type"], "Note")
        self.assertNotIn("type", legacy["data"][0])
        self.assertEqual([_path(call["url"]) for call in self.transport.calls], ["/Tenants/9/History"])

    def test_open_receivables_use_amount_due(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(_path(url), "/Tenants/7/OpenReceivables")
            return FakeResponse(
                200,
                body=[
                    {
                        "ChargeID": 3,
                        "OriginalAmount": 100,
                        "AmountPaid": 40,
                        "AmountDue": 60,
                        "TransactionDate": "2024-02-01T00:00:00",
                        "ChargeTypeDescription": "Rent",
                        "UnitName": "1A",
                        "Comment": "February",
                    }
                ],
                headers={"X-Total-Results": "1"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_tenant_open_receivables(7))
        self.assertTrue(raw["ok"], raw)
        line = raw["data"][0]
        self.assertEqual(line["balance"], 60)
        self.assertEqual(line["amount_due"], 60)
        self.assertEqual(line["original_amount"], 100)
        self.assertEqual(line["amount_paid"], 40)
        self.assertEqual(line["charge_type_description"], "Rent")
        self.assertEqual(line["unit_name"], "1A")
        self.assertNotEqual(line["balance"], 20)
        self.assertFalse(any(_path(call["url"]).endswith("/Balance") for call in self.transport.calls))
        self.assertEqual(self.transport.auth_calls, 0)

        def wrapped(method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                200,
                body={"OpenReceivables": [{"ID": 4, "AmountDue": 15, "TransactionDate": "2023-01-01"}]},
                headers={"X-Total-Results": "1"},
            )

        self.transport.on_api = wrapped
        raw = json.loads(self.server.rm_tenant_open_receivables(7))
        self.assertEqual(raw["data"][0]["charge_id"], 4)
        self.assertEqual(raw["data"][0]["balance"], 15)

    def test_recurring_charges_for_tenant_or_unit(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            self.assertEqual(kwargs["params"]["embeds"], "ChargeType")
            if path == "/Tenants/7/RecurringCharges":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "RecurringChargeID": 2,
                            "Amount": 1000,
                            "FromDate": "2020-01-01",
                            "Comment": "base",
                            "ChargeType": {
                                "Name": "RC",
                                "Description": "Rent Charge",
                                "GLAccountID": 88,
                                "GLAccount": {"Reference": "4311", "Name": "Rents Received"},
                            },
                        },
                        {
                            "RecurringChargeID": 3,
                            "Amount": 40,
                            "FromDate": "2020-01-01",
                            "ToDate": "2000-01-01",
                            "Comment": "old water",
                        },
                        {
                            "RecurringChargeID": 4,
                            "Amount": 15,
                            "FromDate": "2099-01-01",
                            "Comment": "future parking",
                        },
                    ],
                    headers={"X-Total-Results": "3"},
                )
            if path == "/Units/8/RecurringCharges":
                return FakeResponse(
                    200,
                    body=[{"RecurringChargeID": 9, "Amount": 25, "FromDate": "2020-01-01", "Comment": "Parking"}],
                    headers={"X-Total-Results": "1"},
                )
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_list_recurring_charges(tenant_id=7, current_only=True))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual([row["recurring_charge_id"] for row in raw["data"]], [2])
        self.assertEqual(raw["data"][0]["rent_class"], "base_rent")
        self.assertEqual(raw["data"][0]["charge_type_name"], "RC")
        self.assertEqual(raw["data"][0]["gl_account"], "4311 Rents Received")
        self.assertEqual(raw["data"][0]["gl_account_id"], 88)
        self.assertTrue(raw["data"][0]["is_current"])
        self.assertNotIn("is_rent", raw["data"][0])

        unit = json.loads(self.server.rm_list_recurring_charges(unit_id=8))
        self.assertTrue(unit["ok"], unit)
        self.assertEqual(unit["data"][0]["amount"], 25)
        self.assertEqual(unit["data"][0]["rent_class"], "other")

        self.transport.calls.clear()
        self.transport.on_api = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called"))
        both = json.loads(self.server.rm_list_recurring_charges(tenant_id=7, unit_id=8))
        neither = json.loads(self.server.rm_list_recurring_charges())
        self.assertFalse(both["ok"])
        self.assertFalse(neither["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_rent_roll_assembly(self) -> None:
        today = date(2026, 11, 3)
        units = [
            {
                "UnitID": 1,
                "Name": "A",
                "PropertyID": 4,
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 20,
                        "Amount": 800,
                        "Comment": "November",
                        "ChargeType": {"Name": "RC"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 23,
                        "Amount": 10,
                        "Comment": "storage closet rental",
                        "ChargeType": {"Name": "STO"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 24,
                        "Amount": 5,
                        "Comment": "current month",
                        "ChargeType": {"Name": "MISC"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 21,
                        "Amount": 25,
                        "Comment": "Pet",
                        "FromDate": "2020-01-01",
                        "ToDate": "2026-11-02",
                    },
                    {
                        "RecurringChargeID": 22,
                        "Amount": 30,
                        "Comment": "Storage",
                        "FromDate": "2026-12-01",
                    },
                ],
            },
            {
                "UnitID": 2,
                "Name": "B",
                "PropertyID": 4,
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 10,
                        "Amount": 1,
                        "Comment": "ignored duplicate",
                        "ChargeType": {"Name": "RC"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 11,
                        "Amount": 50,
                        "Comment": "window AC",
                        "ChargeType": {"Name": "RAC"},
                        "FromDate": "2020-01-01",
                    },
                ],
            },
            {
                "UnitID": 3,
                "Name": "C",
                "PropertyID": 4,
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 14,
                        "Amount": 900,
                        "Comment": "Market",
                        "ChargeType": {"Name": "Rent"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 15,
                        "Amount": 12,
                        "Comment": "insurance",
                        "ChargeType": {"Name": "RInsur"},
                        "FromDate": "2020-01-01",
                    },
                ],
            },
            {"UnitID": 4, "Name": "D", "PropertyID": 4},
        ]
        tenants = [
            {
                "TenantID": 7,
                "Name": "Ada",
                "Status": "current",
                "Leases": [{"UnitID": "4", "MoveInDate": "2024-01-01"}],
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 30,
                        "Amount": 1000,
                        "Comment": "current month",
                        "ChargeType": {
                            "Name": "RC",
                            "GLAccount": {"Reference": "4311", "Name": "Rents Received"},
                        },
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 32,
                        "Amount": 200,
                        "Comment": "subsidy",
                        "ChargeType": {"Name": "RC8"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 33,
                        "Amount": -25,
                        "Comment": "incentive",
                        "ChargeType": {"Name": "RCI"},
                        "FromDate": "2020-01-01",
                    },
                    {"RecurringChargeID": 31, "Amount": 40, "Comment": "Water", "FromDate": "2020-01-01"},
                ],
            },
            {
                "TenantID": 8,
                "Name": "Bea",
                "Status": "Past",
                "Leases": [{"UnitID": 1, "MoveInDate": "2019-01-01"}],
                "RecurringCharges": [
                    {"RecurringChargeID": 40, "Amount": 500, "Comment": "Rent", "FromDate": "2020-01-01"}
                ],
            },
            {
                "TenantID": 9,
                "Name": "Cy",
                "Status": "Current",
                "Leases": [
                    {"UnitID": 2, "MoveInDate": "2025-06-01"},
                    {"UnitID": 2, "MoveInDate": "2020-01-01", "MoveOutDate": "2021-01-01"},
                ],
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 10,
                        "Amount": 1100,
                        "Comment": "Rent",
                        "ChargeType": {"Name": "rc"},
                        "FromDate": "2020-01-01",
                    },
                    {
                        "RecurringChargeID": 16,
                        "Amount": 35,
                        "Comment": "renters insurance",
                        "ChargeType": {"Name": "RENTIN"},
                        "FromDate": "2020-01-01",
                    },
                ],
            },
            {
                "TenantID": 10,
                "Name": "Dee",
                "Status": "Current",
                "Leases": [{"UnitID": 1, "MoveInDate": "2018-01-01", "MoveOutDate": "2019-01-01"}],
            },
            {
                "TenantID": 11,
                "Name": "Older",
                "Status": "Current",
                "Leases": [{"UnitID": 2, "MoveInDate": "2024-01-01"}],
                "RecurringCharges": [
                    {"RecurringChargeID": 50, "Amount": 1, "Comment": "Rent", "FromDate": "2020-01-01"}
                ],
            },
        ]
        rows = _assemble_rent_roll(units, tenants, {9: (80.5, "number"), 7: (0.0, "number")}, today=today)
        by_name = {row["unit_name"]: row for row in rows}
        self.assertEqual(list(by_name), ["A", "B", "C", "D"])

        self.assertIsNone(by_name["A"]["tenant"])
        self.assertIsNone(by_name["A"]["balance"])
        self.assertEqual(
            [charge["recurring_charge_id"] for charge in by_name["A"]["recurring_charges"]],
            [20, 23, 24],
        )
        self.assertEqual(by_name["A"]["base_rent"], 800)
        self.assertEqual(by_name["A"]["other_recurring"], 15)
        self.assertEqual(by_name["A"]["recurring_total"], 815)
        self.assertEqual(by_name["A"]["surcharges"], 0)
        self.assertEqual(by_name["A"]["concessions"], 0)
        self.assertEqual(by_name["A"]["charge_source"], "unit")
        classes = {charge["recurring_charge_id"]: charge["rent_class"] for charge in by_name["A"]["recurring_charges"]}
        self.assertEqual(classes, {20: "base_rent", 23: "other", 24: "other"})

        occupied = by_name["B"]
        self.assertEqual(occupied["tenant"]["tenant_id"], 9)
        self.assertEqual(occupied["tenant"]["name"], "Cy")
        self.assertEqual(occupied["balance"], 80.5)
        self.assertEqual(occupied["balance_field"], "number")
        self.assertEqual(occupied["balance_source"], "GET /Tenants/9/Balance")
        self.assertEqual(
            [charge["recurring_charge_id"] for charge in occupied["recurring_charges"]],
            [10, 16, 11],
        )
        self.assertEqual(occupied["recurring_charges"][0]["amount"], 1100)
        self.assertEqual(occupied["base_rent_tenant"], 1100)
        self.assertEqual(occupied["base_rent_subsidized"], 0)
        self.assertEqual(occupied["base_rent"], 1100)
        self.assertEqual(occupied["surcharges"], 50)
        self.assertEqual(occupied["other_recurring"], 35)
        self.assertEqual(occupied["recurring_total"], 1185)
        self.assertEqual(occupied["charge_source"], "tenant+unit")
        self.assertNotEqual(occupied["balance"], occupied["base_rent"])
        self.assertEqual(
            {charge["charge_type_name"]: charge["rent_class"] for charge in occupied["recurring_charges"]},
            {"rc": "base_rent", "RENTIN": "other", "RAC": "surcharge"},
        )

        self.assertIsNone(by_name["C"]["tenant"])
        self.assertEqual(by_name["C"]["base_rent"], 0)
        self.assertEqual(by_name["C"]["other_recurring"], 912)
        self.assertEqual(
            {charge["charge_type_name"]: charge["rent_class"] for charge in by_name["C"]["recurring_charges"]},
            {"Rent": "other", "RInsur": "other"},
        )

        ada = by_name["D"]
        self.assertEqual(ada["tenant"]["name"], "Ada")
        self.assertEqual(ada["balance"], 0.0)
        self.assertEqual(ada["charge_source"], "tenant")
        self.assertEqual(ada["base_rent_tenant"], 1000)
        self.assertEqual(ada["base_rent_subsidized"], 200)
        self.assertEqual(ada["base_rent"], 1200)
        self.assertEqual(ada["concessions"], -25)
        self.assertEqual(ada["other_recurring"], 40)
        self.assertEqual(ada["recurring_total"], 1215)
        rc = next(charge for charge in ada["recurring_charges"] if charge["charge_type_name"] == "RC")
        self.assertEqual(rc["comment"], "current month")
        self.assertEqual(rc["rent_class"], "base_rent")
        self.assertEqual(rc["gl_account"], "4311 Rents Received")

    def test_rent_roll_uses_embeds_and_one_balance_per_tenant(self) -> None:
        units = [
            {
                "UnitID": 1,
                "Name": "1",
                "PropertyID": 4,
                "RecurringCharges": [
                    {"RecurringChargeID": 1, "Amount": 10, "Comment": "Parking", "FromDate": "2020-01-01"}
                ],
            },
            {"UnitID": 2, "Name": "2", "PropertyID": 4, "RecurringCharges": []},
            {"UnitID": 3, "Name": "3", "PropertyID": 4},
        ]
        tenants = [
            {
                "TenantID": 7,
                "Name": "Ada",
                "Status": "Current",
                "Leases": [
                    {"UnitID": 1, "MoveInDate": "2024-01-01"},
                    {"UnitID": 2, "MoveInDate": "2024-02-01"},
                ],
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 2,
                        "Amount": 1000,
                        "Comment": "current month",
                        "ChargeType": {"Name": "RC"},
                        "FromDate": "2020-01-01",
                    }
                ],
            },
            {
                "TenantID": 8,
                "Name": "Bea",
                "Status": "Past",
                "Leases": [{"UnitID": 3, "MoveInDate": "2019-01-01"}],
                "RecurringCharges": [
                    {
                        "RecurringChargeID": 9,
                        "Amount": 500,
                        "Comment": "Rent",
                        "ChargeType": {"Name": "RC"},
                        "FromDate": "2020-01-01",
                    }
                ],
            },
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Units":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                self.assertEqual(params["embeds"], _UNIT_RENT_EMBEDS)
                self.assertEqual(params["embeds"], "RecurringCharges")
                self.assertNotIn("RecurringCharges.ChargeType", params["embeds"])
                self.assertNotIn(";", params["filters"])
                return FakeResponse(200, body=units, headers={"X-Total-Results": "3"})
            if path == "/Tenants":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                self.assertNotIn("Status", params["filters"])
                self.assertEqual(params["embeds"], _TENANT_RENT_EMBEDS)
                self.assertEqual(params["embeds"], "Leases,RecurringCharges")
                self.assertNotIn("RecurringCharges.ChargeType", params["embeds"])
                return FakeResponse(200, body=tenants, headers={"X-Total-Results": "2"})
            if path == "/Tenants/7/Balance":
                self.assertIsNone(kwargs.get("params"))
                return FakeResponse(200, body=80.5)
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_rent_roll(4))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual([_path(call["url"]) for call in self.transport.calls], ["/Units", "/Tenants", "/Tenants/7/Balance"])
        self.assertEqual(raw["meta"]["balance_requests"], 1)
        self.assertEqual(raw["meta"]["occupied"], 2)
        self.assertEqual(raw["meta"]["vacant"], 1)
        self.assertIn("/Tenants/{id}/Balance", raw["meta"]["rent_note"])
        by_name = {row["unit_name"]: row for row in raw["data"]}
        self.assertEqual(by_name["1"]["balance"], 80.5)
        self.assertEqual(by_name["2"]["balance"], 80.5)
        self.assertEqual(by_name["1"]["base_rent"], 1000)
        self.assertEqual(by_name["2"]["base_rent"], 1000)
        self.assertEqual(by_name["1"]["other_recurring"], 10)
        self.assertIsNone(by_name["3"]["tenant"])
        self.assertIsNone(by_name["3"]["balance"])
        self.assertEqual(by_name["3"]["base_rent"], 0)
        self.assertNotEqual(by_name["1"]["balance"], by_name["1"]["base_rent"])
        self.assertFalse(raw["meta"]["truncated"])
        self.assertEqual(self.transport.auth_calls, 0)

        self.transport.calls.clear()
        skipped = json.loads(self.server.rm_rent_roll(4, include_balances=False))
        self.assertTrue(skipped["ok"], skipped)
        self.assertEqual([_path(call["url"]) for call in self.transport.calls], ["/Units", "/Tenants"])
        self.assertEqual(skipped["meta"]["balance_requests"], 0)
        self.assertIsNone(skipped["data"][0]["balance"])
        self.assertEqual(skipped["data"][0]["base_rent"] + skipped["data"][1]["base_rent"], 2000)

        def short(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Units":
                return FakeResponse(200, body=units[:1], headers={"X-Total-Results": "50"})
            if path == "/Tenants":
                return FakeResponse(200, body=tenants, headers={"X-Total-Results": "1"})
            if path == "/Tenants/7/Balance":
                return FakeResponse(200, body=80.5)
            raise AssertionError(path)

        self.transport.on_api = short
        cut = json.loads(self.server.rm_rent_roll(4))
        self.assertTrue(cut["meta"]["truncated"])
        self.assertIn("CUT OFF", cut["meta"]["notice"])
        self.assertEqual(cut["meta"]["unit_total"], 50)

    def test_rent_roll_joins_charge_type_names_without_nested_embed(self) -> None:
        units = [
            {
                "UnitID": 1,
                "Name": "1",
                "PropertyID": 18,
                "RecurringCharges": [
                    {"RecurringChargeID": 2, "ChargeTypeID": 4, "Amount": 15, "FromDate": "2020-01-01"}
                ],
            }
        ]
        tenants = [
            {
                "TenantID": 7,
                "Name": "Ada",
                "Status": "Current",
                "Leases": [{"UnitID": 1, "MoveInDate": "2024-01-01"}],
                "RecurringCharges": [
                    {"RecurringChargeID": 1, "ChargeTypeID": 1, "Amount": 1000, "FromDate": "2020-01-01"}
                ],
            }
        ]
        catalog = [
            {"ChargeTypeID": 1, "Name": "RC", "GLAccountID": 4311},
            {"ChargeTypeID": 4, "Name": "RWD", "GLAccountID": 4311},
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            self.assertNotIn("RecurringCharges.ChargeType", params.get("embeds") or "")
            if path == "/Units":
                self.assertEqual(params["embeds"], "RecurringCharges")
                self.assertEqual(params["filters"], "PropertyID,eq,18")
                return FakeResponse(200, body=units, headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                self.assertEqual(params["embeds"], "Leases,RecurringCharges")
                return FakeResponse(200, body=tenants, headers={"X-Total-Results": "1"})
            if path == "/ChargeTypes":
                self.assertNotIn("filters", params)
                self.assertNotIn("embeds", params)
                return FakeResponse(200, body=catalog, headers={"X-Total-Results": "2"})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_rent_roll(18, include_balances=False))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(
            [_path(call["url"]) for call in self.transport.calls],
            ["/Units", "/Tenants", "/ChargeTypes"],
        )
        row = raw["data"][0]
        self.assertEqual(row["base_rent"], 1000)
        self.assertEqual(row["surcharges"], 15)
        self.assertEqual(row["other_recurring"], 0)
        self.assertEqual(self.transport.auth_calls, 0)

    def test_rent_codes_are_exact_and_comments_do_not_count(self) -> None:
        from rm_mcp.normalize import normalize_recurring_charge

        today = date(2026, 11, 3)
        cases = {
            "RC": "base_rent",
            "rc": "base_rent",
            " RC ": "base_rent",
            "RC8": "subsidized_rent",
            "rc8": "subsidized_rent",
            "RAC": "surcharge",
            "rwd": "surcharge",
            "RCI": "concession",
            "RENTIN": "other",
            "RInsur": "other",
            "REF-Re": "other",
            "RP": "other",
            "STO": "other",
            "EQRC": "other",
            "RAP": "other",
            "Rent": "other",
            "rental": "other",
            "current": "other",
            "parent": "other",
        }
        for code, expected in cases.items():
            dto = normalize_recurring_charge(
                {
                    "RecurringChargeID": 1,
                    "Amount": 10,
                    "FromDate": "2020-01-01",
                    "Comment": "current month",
                    "ChargeType": {"Name": code, "Description": "Rent Charge"},
                },
                today=today,
            )
            self.assertEqual(dto["rent_class"], expected, code)
        comment_only = normalize_recurring_charge(
            {
                "RecurringChargeID": 2,
                "Amount": 10,
                "FromDate": "2020-01-01",
                "Comment": "current month",
            },
            today=today,
        )
        self.assertEqual(comment_only["rent_class"], "other")
        description_only = normalize_recurring_charge(
            {
                "RecurringChargeID": 3,
                "Amount": 10,
                "FromDate": "2020-01-01",
                "Comment": "Rent",
                "ChargeType": {"Description": "Rent Charge"},
            },
            today=today,
        )
        self.assertIsNone(description_only["charge_type_name"])
        self.assertEqual(description_only["rent_class"], "other")

    def test_owner_search_sends_one_filter_and_applies_the_rest_in_python(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            params = kwargs["params"]
            filters = params.get("filters") or ""
            self.assertNotIn(";", filters)
            self.assertEqual(filters, "Name,ct,Oak")
            return FakeResponse(
                200,
                body=[
                    {"OwnerID": 5, "Name": "Oak Trust", "Status": "Current"},
                    {"OwnerID": 6, "Name": "Oak Past", "Status": "Past"},
                ],
                headers={"X-Total-Results": "2"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_find_owner("Oak", status="Current"))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual([row["owner_id"] for row in raw["data"]], [5])
        self.assertEqual(self.transport.auth_calls, 0)
