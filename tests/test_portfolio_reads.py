"""Vacancy outlook, delinquency, and the renewal pipeline. HTTP is mocked."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from test_data_correctness import TOKEN, FakeResponse, Transport, _CREDS, _ENV_KEYS, _header_token

from rm_mcp.client import RMClient
from rm_mcp.ops_reads import LEASE_LIST_EMBEDS
from rm_mcp.portfolio_reads import _VACANT_EMBEDS
from rm_mcp.server import _TENANT_RENT_EMBEDS, _UNIT_RENT_EMBEDS

TODAY = date(2026, 11, 10)


def _path(url: str) -> str:
    return url.split("rm.example.test", 1)[-1].split("?", 1)[0]


def _paths(transport: Transport) -> list[str]:
    return [_path(call["url"]) for call in transport.calls]


def _charge(cid: int, amount: float, code: str, to_date: str | None = None) -> dict[str, Any]:
    return {
        "RecurringChargeID": cid,
        "Amount": amount,
        "FromDate": "2026-01-01",
        "ToDate": to_date,
        "Comment": "RENTIN should not classify this",
        "ChargeType": {"ChargeTypeID": cid, "Name": code, "Description": f"{code} description"},
    }


def _type_ref(cid: int, amount: float, type_id: int) -> dict[str, Any]:
    return {
        "RecurringChargeID": cid,
        "ChargeTypeID": type_id,
        "Amount": amount,
        "FromDate": "2026-01-01",
        "Comment": "RENT should not classify this",
    }


def _ok_page(body: list[dict[str, Any]]) -> FakeResponse:
    return FakeResponse(200, body=body, headers={"X-Total-Results": str(len(body))})


class PortfolioReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "token.json"
        self._env = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
        self._path_patch = unittest.mock.patch("rm_mcp.client.TOKEN_CACHE_PATH", self.cache_path)
        self._path_patch.start()
        self.transport = Transport()
        self._sleep_patch = unittest.mock.patch("rm_mcp.client.time.sleep", return_value=None)
        self._sleep_patch.start()
        self._today_patch = unittest.mock.patch("rm_mcp.ops_reads._today", return_value=TODAY)
        self._today_patch.start()
        import rm_mcp.server as server

        self.server = server
        self._saved_client = server._client
        server._client = None

    def tearDown(self) -> None:
        self.server._client = self._saved_client
        self._today_patch.stop()
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

    def assert_get_only(self) -> None:
        self.assertEqual(self.transport.auth_calls, 0)
        for call in self.transport.calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(_header_token(call["kwargs"].get("headers")), TOKEN)
            blob = json.dumps(call)
            self.assertNotIn("RecurringCharges.ChargeType", blob)
            self.assertNotIn("LeaseRenewals.LeaseRenewalIncreaseOffer", blob)
            self.assertNotIn("OpenBalance", blob)
            params = call["kwargs"].get("params") or {}
            self.assertNotIn("orderby", params)
            self.assertNotIn("fields", params)

    def test_vacancy_outlook_days_vacant_make_ready_and_coming_available(self) -> None:
        units = [
            {
                "UnitID": 32,
                "Name": "32",
                "PropertyID": 4,
                "IsVacant": True,
                "UserDefinedValues": [
                    {"UserDefinedFieldID": 731, "Value": "Yes"},
                    {"UserDefinedFieldID": 732, "Value": "1500"},
                ],
                "OccupancyStatusHistory": [
                    {
                        "TenantID": 7,
                        "OccupancyType": "PastOccupant",
                        "StartDate": "2026-01-01",
                        "EndDate": "2026-09-30",
                    },
                    {
                        "TenantID": 8,
                        "OccupancyType": "PastOccupant",
                        "StartDate": "2026-07-01",
                        "EndDate": "2026-09-30",
                    },
                    {"TenantID": 99, "OccupancyType": "PastOccupant", "StartDate": "2026-10-01"},
                ],
            },
            {
                "UnitID": 40,
                "Name": "40",
                "PropertyID": 4,
                "IsVacant": True,
                "OccupancyStatusHistory": [],
            },
            {
                "UnitID": 22,
                "Name": "22",
                "PropertyID": 4,
                "IsVacant": False,
                "CurrentOccupancyStatus": {
                    "UnitID": 22,
                    "TenantID": 50,
                    "OccupancyType": "CurrentOccupant",
                    "NoticeDate": "2026-10-14",
                    "ExpectedMoveOutDate": "2026-11-14",
                    "EndDate": "2027-01-01",
                },
            },
            {
                "UnitID": 23,
                "Name": "23",
                "PropertyID": 4,
                "IsVacant": False,
                "CurrentOccupancyStatus": {
                    "UnitID": 23,
                    "TenantID": 51,
                    "OccupancyType": "CurrentOccupant",
                    "EndDate": "2026-12-01",
                },
            },
            {
                "UnitID": 24,
                "Name": "24",
                "PropertyID": 4,
                "IsVacant": False,
                "CurrentOccupancyStatus": {
                    "UnitID": 24,
                    "TenantID": 52,
                    "OccupancyType": "CurrentOccupant",
                    "NoticeDate": "2026-11-01",
                    "ExpectedMoveOutDate": "2027-05-01",
                },
            },
            {
                "UnitID": 25,
                "Name": "25",
                "PropertyID": 4,
                "IsVacant": False,
                "CurrentOccupancyStatus": {"UnitID": 25, "TenantID": 53, "OccupancyType": "CurrentOccupant"},
            },
            {"UnitID": 26, "Name": "26", "PropertyID": 4},
        ]
        statuses = [
            {
                "UnitStatusID": 1,
                "UnitID": 32,
                "FromDate": "2026-09-30",
                "ToDate": "2026-10-05",
                "UnitStatusType": {"Name": "MR-OLD", "Description": "old"},
            },
            {
                "UnitStatusID": 2,
                "UnitID": 32,
                "FromDate": "2026-10-06",
                "ToDate": None,
                "Comment": "paint",
                "UnitStatusType": {"Name": "MR-AP", "Description": "MR Owner Appr Pend", "IsVacant": True},
            },
            {
                "UnitStatusID": 3,
                "UnitID": 40,
                "FromDate": "2026-01-01",
                "ToDate": "2026-02-01",
                "UnitStatusType": {"Name": "OLD", "Description": "done"},
            },
        ]
        posts = [
            {
                "MakeReadyPostID": 9,
                "UnitID": 32,
                "StartDate": "2026-10-06",
                "CompletionDate": None,
                "Comments": "paint",
            }
        ]
        tenants = [
            {"TenantID": 50, "Name": "Ada Notice"},
            {"TenantID": 51, "Name": "Bo End"},
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = _path(url)
            params = kwargs.get("params") or {}
            self.assertNotIn("orderby", params)
            self.assertNotIn("fields", params)
            if path == "/Units":
                self.assertEqual(params["filters"], "Property.IsActive,eq,true;PropertyID,eq,4")
                self.assertNotIn("IsVacant", params["filters"])
                self.assertEqual(params["embeds"], _VACANT_EMBEDS)
                self.assertNotIn("CurrentUnitStatus", params["embeds"])
                return _ok_page(units)
            if path == "/Properties":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Units/UnitStatuses":
                self.assertEqual(params["filters"], "UnitID,in,(32,40)")
                self.assertEqual(params["embeds"], "UnitStatusType")
                return _ok_page(statuses)
            if path == "/MakeReadyPosts":
                self.assertNotIn("filters", params)
                return _ok_page(posts)
            if path == "/Tenants":
                self.assertEqual(params["filters"], "TenantID,in,(50,51)")
                self.assertNotIn("embeds", params)
                return _ok_page(tenants)
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_vacancy_outlook(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertFalse(raw["meta"]["truncated"])
        self.assertNotIn("notice", raw["meta"])
        self.assertEqual(raw["meta"]["within_days"], 60)
        self.assertEqual(raw["meta"]["unclassified"], 1)
        self.assertEqual(raw["meta"]["unit_status_mode"], "collection")
        self.assertEqual(raw["meta"]["vacant_count"], 2)
        self.assertEqual(raw["meta"]["coming_available_count"], 2)
        self.assertIn("EndDate", raw["meta"]["vacancy_note"])
        vacant = raw["data"]["vacant"]
        self.assertEqual([row["unit_id"] for row in vacant], [32, 40])
        first = vacant[0]
        self.assertEqual(first["days_vacant"], 41)
        self.assertEqual(first["days_vacant"], (TODAY - date(2026, 9, 30)).days)
        self.assertEqual(first["last_move_out"], "2026-09-30")
        self.assertEqual(first["last_tenant_id"], 8)
        self.assertEqual(first["property_name"], "Oak")
        self.assertEqual(first["make_ready_status"], "MR-AP")
        self.assertEqual(first["make_ready_description"], "MR Owner Appr Pend")
        self.assertEqual(first["make_ready_since"], "2026-10-06")
        self.assertEqual(first["make_ready_comment"], "paint")
        self.assertEqual(first["asking_rent"], 1500.0)
        self.assertTrue(first["list_for_lease"])
        self.assertEqual(first["make_ready_post"]["make_ready_post_id"], 9)
        self.assertTrue(first["make_ready_post"]["open"])
        self.assertIsNone(vacant[1]["days_vacant"])
        self.assertIsNone(vacant[1]["last_move_out"])
        self.assertIsNone(vacant[1]["make_ready_status"])
        self.assertIsNone(vacant[1]["make_ready_post"])
        coming = raw["data"]["coming_available"]
        self.assertEqual([row["unit_id"] for row in coming], [22, 23])
        notice = coming[0]
        self.assertEqual(notice["tenant_id"], 50)
        self.assertEqual(notice["tenant_name"], "Ada Notice")
        self.assertEqual(notice["notice_date"], "2026-10-14")
        self.assertEqual(notice["expected_move_out"], "2026-11-14")
        self.assertEqual(notice["available_on"], "2026-11-14")
        self.assertEqual(notice["days_until_available"], 4)
        self.assertEqual(notice["availability_from"], "expected_move_out")
        ended = coming[1]
        self.assertEqual(ended["tenant_name"], "Bo End")
        self.assertEqual(ended["available_on"], "2026-12-01")
        self.assertEqual(ended["days_until_available"], (date(2026, 12, 1) - TODAY).days)
        self.assertEqual(ended["availability_from"], "occupancy_end")
        self.assertIsNone(ended["notice_date"])
        summary = raw["data"]["by_property"]
        self.assertEqual(summary, [{"property_id": 4, "property_name": "Oak", "vacant": 2, "coming_available": 2}])
        self.assertEqual(
            _paths(self.transport),
            ["/Units", "/Properties", "/Units/UnitStatuses", "/MakeReadyPosts", "/Tenants"],
        )

    def test_vacancy_outlook_truncation_notice(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Units":
                self.assertEqual(params["filters"], "Property.IsActive,eq,true")
                self.assertNotIn(";", params["filters"])
                return FakeResponse(
                    200,
                    body=[
                        {
                            "UnitID": 32,
                            "Name": "32",
                            "PropertyID": 4,
                            "IsVacant": True,
                            "OccupancyStatusHistory": [
                                {"TenantID": 8, "EndDate": "2026-09-30", "OccupancyType": "PastOccupant"}
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "500"},
                )
            if path == "/Properties":
                self.assertEqual(params["filters"], "IsActive,eq,true")
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Units/UnitStatuses":
                return _ok_page([])
            if path == "/MakeReadyPosts":
                return _ok_page([])
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_vacancy_outlook())
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertTrue(raw["meta"]["truncated"])
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertEqual(raw["data"]["vacant"][0]["days_vacant"], 41)
        self.assertIn("not the full", raw["meta"]["notice"])

    def test_vacancy_outlook_permission_error_propagates(self) -> None:
        def denied(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(_path(url), "/Units")
            return FakeResponse(403, text="Insufficient privileges")

        self.make_client()
        self.transport.on_api = denied
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_vacancy_outlook())
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"]["type"], "RMForbiddenError")
        self.assertEqual(raw["error"]["status"], 403)
        self.assertIn("Insufficient privileges", raw["error"]["message"])
        self.assertNotIn("data", raw)
        self.assertEqual(_paths(self.transport), ["/Units"])
        self.assertEqual(self.transport.auth_calls, 0)

        def status_denied(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Units":
                return _ok_page([{"UnitID": 32, "Name": "32", "PropertyID": 4, "IsVacant": True}])
            if path == "/Properties":
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Units/UnitStatuses":
                return FakeResponse(403, text="Insufficient privileges to read unit statuses")
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = status_denied
        blocked = json.loads(self.server.rm_vacancy_outlook())
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["status"], 403)
        self.assertNotIn("data", blocked)
        self.assertEqual(_paths(self.transport), ["/Units", "/Properties", "/Units/UnitStatuses"])
        self.assertFalse(any("/Units/32/UnitStatuses" in path for path in _paths(self.transport)))

    def test_vacancy_outlook_status_404_falls_back_to_per_unit(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Units":
                return _ok_page(
                    [
                        {
                            "UnitID": 32,
                            "Name": "32",
                            "PropertyID": 4,
                            "IsVacant": True,
                            "OccupancyStatusHistory": [{"TenantID": 8, "EndDate": "2026-09-30"}],
                        }
                    ]
                )
            if path == "/Properties":
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Units/UnitStatuses":
                return FakeResponse(404, text='{"Message":"KeyNotFoundException"}')
            if path == "/Units/32/UnitStatuses":
                self.assertEqual(params["embeds"], "UnitStatusType")
                return _ok_page(
                    [
                        {
                            "UnitStatusID": 2,
                            "UnitID": 32,
                            "FromDate": "2026-10-06",
                            "Comment": "paint",
                            "UnitStatusType": {"Name": "MR-AP", "Description": "MR Owner Appr Pend"},
                        }
                    ]
                )
            if path == "/MakeReadyPosts":
                return FakeResponse(404, text="not found")
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_vacancy_outlook(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertEqual(raw["meta"]["unit_status_mode"], "per_unit")
        self.assertIn("404", raw["meta"]["make_ready_note"])
        row = raw["data"]["vacant"][0]
        self.assertEqual(row["make_ready_status"], "MR-AP")
        self.assertEqual(row["make_ready_since"], "2026-10-06")
        self.assertIsNone(row["make_ready_post"])
        self.assertEqual(row["days_vacant"], 41)
        self.assertEqual(
            _paths(self.transport),
            ["/Units", "/Properties", "/Units/UnitStatuses", "/Units/32/UnitStatuses", "/MakeReadyPosts"],
        )

    def test_delinquency_summary_groups_aging_balance_and_rent(self) -> None:
        ninety = (TODAY - timedelta(days=90)).isoformat()
        ninety_one = (TODAY - timedelta(days=91)).isoformat()
        tenants = [
            {
                "TenantID": 10,
                "Name": "Ada Behind",
                "Status": "Current",
                "PropertyID": 4,
                "Balance": 250.0,
                "Leases": [{"LeaseID": 1, "UnitID": 7, "Unit": {"UnitID": 7, "Name": "A1"}}],
                "OpenReceivables": [
                    {"ChargeID": 1, "TransactionDate": "2026-11-01", "AmountDue": 10, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 2, "TransactionDate": "2026-10-01", "AmountDue": 20, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 3, "TransactionDate": "2026-09-01", "AmountDue": 30, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 4, "TransactionDate": "2026-07-01", "AmountDue": 40, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 5, "TransactionDate": ninety, "AmountDue": 1, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 6, "TransactionDate": ninety_one, "AmountDue": 2, "UnitID": 7, "UnitName": "A1"},
                    {"ChargeID": 7, "AmountDue": 5, "UnitID": 7, "UnitName": "A1"},
                ],
                "RecurringCharges": [
                    _type_ref(1, 1000, 1),
                    _type_ref(2, 200, 2),
                    _type_ref(3, 50, 3),
                ],
            },
            {
                "TenantID": 12,
                "Name": "Mismatch",
                "Status": "Current",
                "PropertyID": 4,
                "Balance": 40.0,
                "Leases": [],
                "OpenReceivables": [],
                "RecurringCharges": [],
            },
            {
                "TenantID": 13,
                "Name": "Cleared",
                "Status": "Current",
                "PropertyID": 5,
                "Balance": 10.0,
                "Leases": [],
                "OpenReceivables": [],
                "RecurringCharges": [],
            },
        ]
        catalog = [
            {"ChargeTypeID": 1, "Name": "RC"},
            {"ChargeTypeID": 2, "Name": "RC8"},
            {"ChargeTypeID": 3, "Name": "RAC"},
        ]
        balances = {10: 250.0, 12: 55.0, 13: 0.0}

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = _path(url)
            params = kwargs.get("params") or {}
            self.assertNotIn("orderby", params)
            self.assertNotIn("fields", params)
            if path == "/Tenants":
                self.assertEqual(params["filters"], "Status,eq,Current;Balance,gt,0;Property.IsActive,eq,true")
                self.assertEqual(params["embeds"], "Balance,OpenReceivables,Leases,RecurringCharges")
                self.assertNotIn("OpenReceivables.ChargeType", params["embeds"])
                self.assertNotIn("RecurringCharges.ChargeType", params["embeds"])
                return _ok_page(tenants)
            if path == "/Properties":
                self.assertEqual(params["filters"], "IsActive,eq,true")
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}, {"PropertyID": 5, "Name": "Pine"}])
            if path == "/Units":
                self.assertEqual(params["filters"], "Property.IsActive,eq,true")
                self.assertEqual(params["embeds"], _UNIT_RENT_EMBEDS)
                return _ok_page(
                    [{"UnitID": 7, "RecurringCharges": [_type_ref(1, 999, 1), _type_ref(11, 10, 1)]}]
                )
            if path == "/ChargeTypes":
                self.assertNotIn("filters", params)
                self.assertNotIn("embeds", params)
                return _ok_page(catalog)
            if path.startswith("/Tenants/") and path.endswith("/Balance"):
                tenant_id = int(path.split("/")[2])
                return FakeResponse(200, body=balances[tenant_id])
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_delinquency_summary(include_rent=True))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertFalse(raw["meta"]["truncated"])
        self.assertNotIn("notice", raw["meta"])
        self.assertEqual(raw["meta"]["balance_requests"], 3)
        self.assertEqual(raw["meta"]["candidate_count"], 3)
        self.assertEqual(raw["meta"]["min_balance"], 0.0)
        self.assertIn("can be missed", raw["meta"]["selection_note"])
        self.assertIn("no due date", raw["meta"]["aging_note"])
        self.assertIn("TransactionDate", raw["meta"]["aging_note"])
        self.assertEqual(
            raw["meta"]["balance_mismatches"],
            [
                {"tenant_id": 12, "embed_balance": 40.0, "endpoint_balance": 55.0},
                {"tenant_id": 13, "embed_balance": 10.0, "endpoint_balance": 0.0},
            ],
        )
        self.assertEqual(raw["meta"]["excluded_after_balance_check"][0]["tenant_id"], 13)
        self.assertEqual(raw["meta"]["excluded_after_balance_check"][0]["balance"], 0.0)
        self.assertEqual(raw["data"]["totals"]["tenant_count"], 2)
        self.assertEqual(raw["data"]["totals"]["total_balance"], 305.0)
        self.assertEqual(len(raw["data"]["properties"]), 1)
        prop = raw["data"]["properties"][0]
        self.assertEqual(prop["property_id"], 4)
        self.assertEqual(prop["property_name"], "Oak")
        self.assertEqual(prop["total_balance"], 305.0)
        self.assertEqual([row["tenant_id"] for row in prop["tenants"]], [10, 12])
        ada = prop["tenants"][0]
        self.assertEqual(ada["balance"], 250.0)
        self.assertEqual(ada["embed_balance"], 250.0)
        self.assertTrue(ada["balance_confirmed"])
        self.assertEqual(ada["balance_source"], "GET /Tenants/10/Balance")
        self.assertEqual(ada["units"], [{"unit_id": 7, "unit_name": "A1"}])
        self.assertEqual(ada["oldest_open_charge_date"], "2026-07-01")
        self.assertEqual(ada["aging"]["0-30"], 10.0)
        self.assertEqual(ada["aging"]["31-60"], 20.0)
        self.assertEqual(ada["aging"]["61-90"], 31.0)
        self.assertEqual(ada["aging"]["90+"], 42.0)
        self.assertEqual(ada["aging"]["undated"], 5.0)
        self.assertEqual(ada["current_rent"], 1210.0)
        self.assertEqual(ada["base_rent_tenant"], 1010.0)
        self.assertEqual(ada["base_rent_subsidized"], 200.0)
        other = prop["tenants"][1]
        self.assertEqual(other["balance"], 55.0)
        self.assertEqual(other["aging"]["0-30"], 0.0)
        self.assertIsNone(other["oldest_open_charge_date"])
        self.assertEqual(other["current_rent"], 0.0)
        self.assertEqual(
            _paths(self.transport),
            [
                "/Tenants",
                "/Properties",
                "/Units",
                "/ChargeTypes",
                "/Tenants/10/Balance",
                "/Tenants/12/Balance",
                "/Tenants/13/Balance",
            ],
        )

    def test_delinquency_summary_filter_variants(self) -> None:
        seen: list[str] = []

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Tenants":
                seen.append(params["filters"])
                self.assertNotIn("fields", params)
                return _ok_page([])
            if path == "/Properties":
                return _ok_page([])
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        scoped = json.loads(self.server.rm_delinquency_summary(property_id=4, min_balance=25))
        self.assertTrue(scoped["ok"], scoped)
        past = json.loads(self.server.rm_delinquency_summary(include_past=True))
        self.assertTrue(past["ok"], past)
        self.assertEqual(
            seen,
            [
                "Status,eq,Current;Balance,gt,25;PropertyID,eq,4",
                "Balance,gt,0;Property.IsActive,eq,true",
            ],
        )
        self.assertNotIn("Status", seen[1])
        self.assertEqual(self.transport.auth_calls, 0)
        self.assert_get_only()

    def test_delinquency_summary_truncation_notice(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Tenants":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "TenantID": 10,
                            "Name": "Ada",
                            "Status": "Current",
                            "PropertyID": 4,
                            "Balance": 80.0,
                            "OpenReceivables": [],
                            "Leases": [],
                        }
                    ],
                    headers={"X-Total-Results": "80"},
                )
            if path == "/Properties":
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Tenants/10/Balance":
                return FakeResponse(200, body=80)
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_delinquency_summary())
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertTrue(raw["meta"]["truncated"])
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertEqual(raw["meta"]["balance_requests"], 1)
        self.assertEqual(raw["data"]["totals"]["total_balance"], 80.0)
        self.assertNotIn("/Units", _paths(self.transport))
        self.assertNotIn("/ChargeTypes", _paths(self.transport))

    def test_delinquency_summary_balance_403_is_not_swallowed(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            if path == "/Tenants":
                self.assertEqual(params["embeds"], "Balance,OpenReceivables,Leases")
                return _ok_page(
                    [
                        {
                            "TenantID": 10,
                            "Name": "Ada",
                            "Status": "Current",
                            "PropertyID": 4,
                            "Balance": 80.0,
                            "OpenReceivables": [],
                            "Leases": [],
                        }
                    ]
                )
            if path == "/Properties":
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/Tenants/10/Balance":
                return FakeResponse(403, text="Insufficient privileges")
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_delinquency_summary())
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"]["type"], "RMForbiddenError")
        self.assertEqual(raw["error"]["status"], 403)
        self.assertIn("Insufficient privileges", raw["error"]["message"])
        self.assertNotIn("data", raw)
        self.assertEqual(_paths(self.transport), ["/Tenants", "/Properties", "/Tenants/10/Balance"])
        self.assertEqual(self.transport.auth_calls, 0)

    def _pipeline_leases(self) -> list[dict[str, Any]]:
        def row(
            lease_id: int,
            tenant_id: int,
            unit_id: int,
            end: str,
            *,
            name: str,
            renewals: list[dict[str, Any]],
            notice: str | None = None,
            expected: str | None = None,
        ) -> dict[str, Any]:
            raw: dict[str, Any] = {
                "LeaseID": lease_id,
                "TenantID": tenant_id,
                "UnitID": unit_id,
                "PropertyID": 4,
                "Tenant": {"TenantID": tenant_id, "Name": name},
                "Unit": {"UnitID": unit_id, "Name": f"U{unit_id}"},
                "LeaseRenewals": renewals,
            }
            if notice:
                raw["NoticeDate"] = notice
            if expected:
                raw["ExpectedMoveOutDate"] = expected
            return raw

        def term(renewal_id: int, end: str, **extra: Any) -> dict[str, Any]:
            payload = {
                "LeaseRenewalID": renewal_id,
                "StartDate": "2026-01-01",
                "EndDate": end,
            }
            payload.update(extra)
            return payload

        return [
            row(
                1,
                10,
                100,
                "2026-12-01",
                name="Ada Tenant",
                notice="2026-10-15",
                expected="2026-11-30",
                renewals=[term(11, "2026-12-01", ParentLeaseID=1)],
            ),
            row(
                5,
                15,
                105,
                "2026-11-18",
                name="On File",
                renewals=[
                    term(51, "2026-11-18", ParentLeaseID=5),
                    {"LeaseRenewalID": 52, "ParentLeaseID": 5, "StartDate": "2026-11-18", "EndDate": "2027-11-18"},
                ],
            ),
            row(
                4,
                14,
                104,
                "2026-11-20",
                name="Signed Next",
                notice="2026-10-01",
                renewals=[
                    term(41, "2026-11-20", ParentLeaseID=4),
                    {
                        "LeaseRenewalID": 42,
                        "ParentLeaseID": 4,
                        "StartDate": "2026-11-20",
                        "EndDate": "2027-11-20",
                        "SignDate": "2026-10-01",
                    },
                ],
            ),
            row(8, 18, 108, "2026-11-25", name="Approved", renewals=[term(81, "2026-11-25", ParentLeaseID=8)]),
            row(9, 19, 109, "2026-11-28", name="Offered", renewals=[term(91, "2026-11-28", ParentLeaseID=9)]),
            row(10, 20, 110, "2027-01-15", name="Unit Notice", renewals=[term(101, "2027-01-15", ParentLeaseID=10)]),
            row(11, 21, 111, "2027-02-01", name="Quiet", renewals=[term(111, "2027-02-01", ParentLeaseID=11)]),
            row(2, 12, 102, "2027-08-01", name="Far", renewals=[term(21, "2027-08-01", ParentLeaseID=2)]),
            {
                "LeaseID": 3,
                "TenantID": 13,
                "UnitID": 103,
                "PropertyID": 4,
                "MoveOutDate": "2026-10-01",
                "LeaseRenewals": [term(31, "2026-11-20")],
            },
            {
                "LeaseID": 6,
                "TenantID": 16,
                "UnitID": 106,
                "PropertyID": 4,
                "Tenant": {"Name": "Open ended"},
                "LeaseRenewals": [],
            },
        ]

    def test_renewal_pipeline_groups_offers_notice_and_rent(self) -> None:
        tenants = [
            {
                "TenantID": 10,
                "RecurringCharges": [
                    _charge(1, 1000, "RC"),
                    _charge(2, 200, "RC8"),
                    _charge(3, 50, "RAC"),
                    _charge(4, -25, "RCI"),
                    _charge(5, 7, "STO"),
                    _charge(6, 999, "RC", to_date="2026-01-01"),
                ],
            }
        ]
        units = [{"UnitID": 100, "RecurringCharges": [_charge(1, 999, "RC"), _charge(11, 10, "RC")]}]
        occupancy = [
            {
                "UnitID": 110,
                "CurrentOccupancyStatus": {
                    "UnitID": 110,
                    "TenantID": 20,
                    "OccupancyType": "CurrentOccupant",
                    "NoticeDate": "2026-11-01",
                },
            }
        ]
        offers = [
            {
                "LeaseRenewalID": 81,
                "ParentLeaseID": 8,
                "StartDate": "2026-01-01",
                "EndDate": "2026-11-25",
                "LeaseRenewalIncreaseOffer": {
                    "LeaseRenewalIncreaseOfferID": 1,
                    "Amount": 50,
                    "ApprovalDate": "2026-10-01",
                },
            },
            {
                "LeaseRenewalID": 91,
                "ParentLeaseID": 9,
                "StartDate": "2026-01-01",
                "EndDate": "2026-11-28",
                "LeaseRenewalIncreaseOffer": {
                    "LeaseRenewalIncreaseOfferID": 2,
                    "Amount": 25,
                    "SetDate": "2026-10-02",
                },
            },
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = _path(url)
            params = kwargs.get("params") or {}
            self.assertNotIn("orderby", params)
            embeds = params.get("embeds") or ""
            self.assertNotIn("RecurringCharges.ChargeType", embeds)
            self.assertNotIn("LeaseRenewals.LeaseRenewalIncreaseOffer", embeds)
            if path == "/Leases":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                self.assertNotIn(";", params["filters"])
                self.assertEqual(params["embeds"], LEASE_LIST_EMBEDS)
                return _ok_page(self._pipeline_leases())
            if path == "/Tenants":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                self.assertEqual(params["embeds"], _TENANT_RENT_EMBEDS)
                return _ok_page(tenants)
            if path == "/Units" and params.get("embeds") == _UNIT_RENT_EMBEDS:
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                return _ok_page(units)
            if path == "/Units" and params.get("embeds") == "CurrentOccupancyStatus":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                return _ok_page(occupancy)
            if path == "/Properties":
                self.assertEqual(params["filters"], "PropertyID,eq,4")
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/LeaseRenewals":
                self.assertEqual(params["filters"], "ParentLeaseID,in,(1,4,5,8,9,10,11)")
                self.assertEqual(params["embeds"], "LeaseRenewalIncreaseOffer")
                return _ok_page(offers)
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_renewal_pipeline(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertFalse(raw["meta"]["truncated"])
        self.assertNotIn("notice", raw["meta"])
        self.assertEqual(raw["meta"]["within_days"], 90)
        self.assertEqual(raw["meta"]["open_ended"], 1)
        self.assertEqual(raw["meta"]["charge_mode"], "property_collections")
        self.assertEqual(raw["meta"]["offer_mode"], "batch")
        self.assertEqual(raw["meta"]["offer_requests"], 1)
        self.assertIn("most advanced first", raw["meta"]["stage_note"])
        self.assertIn("404", raw["meta"]["stage_note"])
        self.assertEqual([row["lease_id"] for row in raw["data"]["leases"]], [5, 4, 8, 9, 1, 10, 11])
        by_id = {row["lease_id"]: row for row in raw["data"]["leases"]}
        ada = by_id[1]
        self.assertEqual(ada["renewal_status"], "no_renewal")
        self.assertEqual(ada["stage"], "notice_given")
        self.assertTrue(ada["notice_given"])
        self.assertEqual(ada["notice_date"], "2026-10-15")
        self.assertEqual(ada["current_rent"], 1210.0)
        self.assertEqual(ada["base_rent_tenant"], 1010.0)
        self.assertEqual(ada["base_rent_subsidized"], 200.0)
        self.assertEqual(ada["surcharges"], 50.0)
        self.assertEqual(ada["concessions"], -25.0)
        self.assertEqual(ada["other_recurring"], 7.0)
        self.assertEqual(ada["property_name"], "Oak")
        self.assertIsNone(ada["increase_offer"])
        signed = by_id[4]
        self.assertEqual(signed["stage"], "signed")
        self.assertEqual(signed["renewal_status"], "signed")
        self.assertTrue(signed["notice_given"])
        self.assertEqual(by_id[5]["stage"], "renewal_on_file")
        self.assertEqual(by_id[5]["renewal_status"], "renewal_on_file")
        approved = by_id[8]
        self.assertEqual(approved["stage"], "offer_approved")
        self.assertEqual(approved["increase_offer"]["offer_status"], "approved")
        self.assertEqual(approved["increase_offer"]["amount"], 50.0)
        self.assertEqual(approved["offer_on_renewal_id"], 81)
        offered = by_id[9]
        self.assertEqual(offered["stage"], "offer_set")
        self.assertEqual(offered["increase_offer"]["offer_status"], "set")
        self.assertEqual(offered["increase_offer"]["amount"], 25.0)
        unit_notice = by_id[10]
        self.assertIsNone(unit_notice["notice_date"])
        self.assertEqual(unit_notice["occupancy_notice_date"], "2026-11-01")
        self.assertTrue(unit_notice["notice_given"])
        self.assertEqual(unit_notice["stage"], "notice_given")
        quiet = by_id[11]
        self.assertFalse(quiet["notice_given"])
        self.assertEqual(quiet["stage"], "no_renewal")
        self.assertIsNone(quiet["current_rent"])
        counts = raw["data"]["counts"]
        self.assertEqual(counts["leases"], 7)
        self.assertEqual(counts["notice_given"], 3)
        self.assertEqual(counts["with_offer"], 2)
        self.assertEqual(counts["by_status"], {"no_renewal": 5, "renewal_on_file": 1, "signed": 1})
        self.assertEqual(
            counts["by_stage"],
            {
                "signed": 1,
                "renewal_on_file": 1,
                "offer_approved": 1,
                "offer_set": 1,
                "notice_given": 2,
                "no_renewal": 1,
            },
        )
        self.assertEqual(raw["data"]["by_property"][0]["property_name"], "Oak")
        self.assertEqual(raw["data"]["by_property"][0]["count"], 7)
        self.assertEqual(raw["data"]["by_property"][0]["by_status"], counts["by_status"])
        self.assertEqual(
            _paths(self.transport),
            ["/Leases", "/Tenants", "/Units", "/Units", "/Properties", "/LeaseRenewals"],
        )

    def test_renewal_pipeline_offer_batch_404_falls_back(self) -> None:
        def lease(lease_id: int, renewal_id: int, end: str) -> dict[str, Any]:
            return {
                "LeaseID": lease_id,
                "TenantID": lease_id,
                "UnitID": lease_id,
                "PropertyID": 4,
                "Tenant": {"Name": f"T{lease_id}"},
                "Unit": {"Name": f"U{lease_id}"},
                "LeaseRenewals": [
                    {
                        "LeaseRenewalID": renewal_id,
                        "ParentLeaseID": lease_id,
                        "StartDate": "2026-01-01",
                        "EndDate": end,
                    }
                ],
            }

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            params = kwargs.get("params") or {}
            embeds = params.get("embeds") or ""
            self.assertNotIn("LeaseRenewals.LeaseRenewalIncreaseOffer", embeds)
            self.assertNotIn("LeaseRenewals.LeaseRenewalIncreaseOffer", path)
            if path == "/Leases":
                return _ok_page([lease(8, 81, "2026-11-25"), lease(11, 111, "2027-02-01")])
            if path == "/Tenants":
                return _ok_page([])
            if path == "/Units":
                return _ok_page([])
            if path == "/Properties":
                return _ok_page([{"PropertyID": 4, "Name": "Oak"}])
            if path == "/LeaseRenewals":
                self.assertEqual(params["filters"], "ParentLeaseID,in,(8,11)")
                self.assertEqual(params["embeds"], "LeaseRenewalIncreaseOffer")
                return FakeResponse(404, text='{"Message":"KeyNotFoundException"}')
            if path == "/LeaseRenewals/81/LeaseRenewalIncreaseOffer":
                return FakeResponse(
                    200,
                    body={
                        "LeaseRenewalIncreaseOfferID": 7,
                        "Amount": 75,
                        "ApprovalDate": "2026-10-01",
                    },
                )
            if path == "/LeaseRenewals/111/LeaseRenewalIncreaseOffer":
                return FakeResponse(404, text='{"Message":"KeyNotFoundException"}')
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_renewal_pipeline(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertEqual(raw["meta"]["offer_mode"], "per_renewal")
        self.assertEqual(raw["meta"]["offer_requests"], 2)
        by_id = {row["lease_id"]: row for row in raw["data"]["leases"]}
        self.assertEqual(by_id[8]["stage"], "offer_approved")
        self.assertEqual(by_id[8]["increase_offer"]["amount"], 75.0)
        self.assertEqual(by_id[8]["offer_on_renewal_id"], 81)
        self.assertEqual(by_id[11]["stage"], "no_renewal")
        self.assertIsNone(by_id[11]["increase_offer"])
        self.assertEqual(
            _paths(self.transport)[-3:],
            [
                "/LeaseRenewals",
                "/LeaseRenewals/81/LeaseRenewalIncreaseOffer",
                "/LeaseRenewals/111/LeaseRenewalIncreaseOffer",
            ],
        )

    def test_renewal_pipeline_offer_403_is_not_swallowed(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Leases":
                return _ok_page(
                    [
                        {
                            "LeaseID": 8,
                            "TenantID": 18,
                            "UnitID": 108,
                            "PropertyID": 4,
                            "Tenant": {"Name": "Ada"},
                            "Unit": {"Name": "H8"},
                            "LeaseRenewals": [
                                {
                                    "LeaseRenewalID": 81,
                                    "ParentLeaseID": 8,
                                    "StartDate": "2026-01-01",
                                    "EndDate": "2026-11-25",
                                }
                            ],
                        }
                    ]
                )
            if path in {"/Tenants", "/Units", "/Properties"}:
                return _ok_page([])
            if path == "/LeaseRenewals":
                return FakeResponse(403, text="Insufficient privileges")
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_renewal_pipeline(property_id=4))
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"]["type"], "RMForbiddenError")
        self.assertEqual(raw["error"]["status"], 403)
        self.assertIn("Insufficient privileges", raw["error"]["message"])
        self.assertNotIn("data", raw)
        self.assertNotIn("/LeaseRenewals/81/LeaseRenewalIncreaseOffer", _paths(self.transport))
        self.assertEqual(self.transport.auth_calls, 0)

    def test_renewal_pipeline_truncation_notice(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = _path(url)
            if path == "/Leases":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "LeaseID": 11,
                            "TenantID": 21,
                            "UnitID": 111,
                            "PropertyID": 4,
                            "Tenant": {"Name": "Quiet"},
                            "Unit": {"Name": "K11"},
                            "LeaseRenewals": [
                                {
                                    "LeaseRenewalID": 111,
                                    "ParentLeaseID": 11,
                                    "StartDate": "2026-01-01",
                                    "EndDate": "2027-02-01",
                                }
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "40"},
                )
            if path in {"/Tenants", "/Units", "/Properties"}:
                return _ok_page([])
            if path == "/LeaseRenewals":
                return _ok_page([])
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_renewal_pipeline(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assert_get_only()
        self.assertTrue(raw["meta"]["truncated"])
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertIn("Counts describe the leases included", raw["meta"]["notice"])
        self.assertEqual(raw["data"]["counts"]["leases"], 1)
        self.assertEqual(raw["data"]["leases"][0]["stage"], "no_renewal")
