"""Read-only prospect, renewal, agenda, and report tools. HTTP is mocked."""

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
from rm_mcp.ops_reads import (
    APPOINTMENT_EMBEDS,
    LEASE_DETAIL_EMBEDS,
    LEASE_LIST_EMBEDS,
    MEMORIZED_LIST_EMBEDS,
    PROSPECT_DETAIL_EMBEDS,
    PROSPECT_LIST_EMBEDS,
    REPORT_LIST_EMBEDS,
    TASK_EMBEDS,
    summarize_report_body,
)
from rm_mcp.server import _TENANT_RENT_EMBEDS, _UNIT_RENT_EMBEDS

TODAY = date(2026, 11, 4)


def _charge(cid: int, amount: float, code: str, to_date: str | None = None) -> dict[str, Any]:
    return {
        "RecurringChargeID": cid,
        "Amount": amount,
        "FromDate": "2026-01-01",
        "ToDate": to_date,
        "Comment": "RENTIN should not classify this",
        "ChargeType": {"ChargeTypeID": cid, "Name": code, "Description": f"{code} description"},
    }


def _lease_row(lease_id: int, tenant_id: int, unit_id: int, end: str = "2026-12-01") -> dict[str, Any]:
    return {
        "LeaseID": lease_id,
        "TenantID": tenant_id,
        "UnitID": unit_id,
        "PropertyID": 18,
        "Tenant": {"TenantID": tenant_id, "Name": f"Tenant {tenant_id}"},
        "Unit": {"UnitID": unit_id, "Name": f"U{unit_id}"},
        "LeaseRenewals": [
            {"LeaseRenewalID": lease_id * 10, "StartDate": "2025-12-01", "EndDate": end}
        ],
    }


def _type_ref(
    cid: int,
    amount: float,
    type_id: int | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "RecurringChargeID": cid,
        "Amount": amount,
        "FromDate": "2026-01-01",
        "ToDate": to_date,
        "Comment": "RENT should not classify this",
    }
    if type_id is not None:
        row["ChargeTypeID"] = type_id
    return row


def _paths(transport: Transport) -> list[str]:
    return [call["url"].split("rm.example.test", 1)[-1] for call in transport.calls]


class Batch2ReadTests(unittest.TestCase):
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

    def test_search_prospects_uses_one_id_filter_and_python_for_the_rest(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertEqual(_header_token(kwargs.get("headers")), TOKEN)
            params = kwargs["params"]
            self.assertEqual(params["filters"], "PropertyID,eq,4")
            self.assertNotIn(";", params["filters"])
            self.assertNotIn("orderby", params)
            self.assertEqual(params["embeds"], PROSPECT_LIST_EMBEDS)
            self.assertNotIn("fields", params)
            rows = [
                {
                    "ProspectID": 1,
                    "ProspectDisplayID": 101,
                    "PropertyID": 4,
                    "Name": "Ada Guest",
                    "ProspectStatus": "Prospect",
                    "CurrentProspectStageID": 3,
                    "LeadSourceID": 8,
                    "FirstContact": "2026-10-01T08:00:00",
                    "CreateDate": "2026-09-01T08:00:00",
                    "Comment": "Called about 2 bed",
                    "CurrentProspectStage": {"ProspectStageID": 3, "Name": "Guest Card"},
                    "LeadSource": {"ProspectLeadSourceID": 8, "Description": "Apartments.com"},
                    "PrimaryContact": {
                        "FirstName": "Ada",
                        "Email": "ada@example.com",
                        "PhoneNumbers": [{"PhoneNumber": "555-0100", "IsPrimary": True}],
                    },
                },
                {
                    "ProspectID": 2,
                    "Name": "Bob Lost",
                    "PropertyID": 4,
                    "ProspectStatus": "Lost",
                    "CurrentProspectStageID": 9,
                    "LeadSourceID": 2,
                    "FirstContact": "2026-11-01T08:00:00",
                    "CreateDate": "2026-11-01T08:00:00",
                    "CurrentProspectStage": {"ProspectStageID": 9, "Name": "Application"},
                    "LeadSource": {"ProspectLeadSourceID": 2, "Description": "Drive-by"},
                },
            ]
            return FakeResponse(200, body=rows, headers={"X-Total-Results": "2"})

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(
            self.server.rm_search_prospects(
                property_id=4,
                query="ada",
                stage="Guest Card",
                status="Prospect",
                lead_source="apartment",
                lead_source_id=8,
                date_on="first_contact",
                date_from="2026-09-01",
                date_to="2026-10-15",
            )
        )
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual([row["prospect_id"] for row in raw["data"]], [1])
        hit = raw["data"][0]
        self.assertEqual(hit["phone"], "555-0100")
        self.assertEqual(hit["email"], "ada@example.com")
        self.assertEqual(hit["stage_name"], "Guest Card")
        self.assertEqual(hit["lead_source"], "Apartments.com")
        self.assertEqual(hit["first_contact"], "2026-10-01")
        self.assertFalse(raw["meta"]["truncated"])
        self.assertEqual(len(self.transport.calls), 1)

        self.transport.calls.clear()
        punct = json.loads(self.server.rm_search_prospects(property_id=4, query="ada;Name,eq,x"))
        self.assertTrue(punct["ok"], punct)
        self.assertEqual(punct["data"], [])
        self.assertEqual(self.transport.calls[-1]["kwargs"]["params"]["filters"], "PropertyID,eq,4")

        self.transport.calls.clear()
        missed = json.loads(self.server.rm_search_prospects(property_id=4, stage="Guest"))
        self.assertTrue(missed["ok"], missed)
        self.assertEqual(missed["data"], [])

        self.transport.calls.clear()
        bad = json.loads(self.server.rm_search_prospects(date_on="move-in"))
        self.assertFalse(bad["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_prospect_detail_and_lookups(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs.get("params") or {}
            self.assertNotIn("orderby", params)
            if path == "/Prospects":
                self.assertEqual(params["filters"], "ProspectID,eq,7")
                self.assertEqual(params["embeds"], PROSPECT_DETAIL_EMBEDS)
                return FakeResponse(
                    200,
                    body=[
                        {
                            "ProspectID": 7,
                            "Name": "Ada Guest",
                            "ProspectStatus": "Lost",
                            "ProspectLostReasonID": 4,
                            "ProspectLostReasonDescription": "Chose another community",
                            "ProspectLostReason": {
                                "ProspectLostReasonID": 4,
                                "Name": "Rented elsewhere",
                                "Description": "Chose another community",
                                "ProspectStatus": "Lost",
                            },
                            "CurrentProspectStage": {"ProspectStageID": 3, "Name": "Guest Card", "Description": "New lead"},
                            "Contacts": [
                                {
                                    "ContactID": 1,
                                    "FirstName": "Ada",
                                    "LastName": "Guest",
                                    "Email": "ada@example.com",
                                    "PhoneNumbers": [{"PhoneNumber": "555-0100", "IsPrimary": True}],
                                }
                            ],
                            "Addresses": [{"AddressID": 2, "Street": "1 Main", "City": "Akron", "State": "OH", "PostalCode": "44301", "IsPrimary": True}],
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Prospects/7/ProspectStageHistory":
                self.assertEqual(params["embeds"], "ProspectStage")
                return FakeResponse(
                    200,
                    body=[
                        {
                            "ProspectStageHistoryID": 1,
                            "ProspectStageID": 3,
                            "FromDate": "2026-10-01T00:00:00",
                            "ToDate": "2026-11-01T00:00:00",
                            "ProspectStage": {"ProspectStageID": 3, "Name": "Guest Card"},
                        },
                        {
                            "ProspectStageHistoryID": 2,
                            "ProspectStageID": 9,
                            "FromDate": "2026-11-01T00:00:00",
                            "ProspectStage": {"ProspectStageID": 9, "Name": "Application"},
                        },
                    ],
                    headers={"X-Total-Results": "2"},
                )
            if path == "/Prospects/7/InterestedRentals":
                self.assertEqual(params["embeds"], "Unit,Property")
                return FakeResponse(
                    200,
                    body=[
                        {
                            "InterestedRentalID": 5,
                            "EntityTypeID": "Unit",
                            "EntityKeyID": 15,
                            "UnitID": 15,
                            "IsWaitingList": False,
                            "Unit": {"UnitID": 15, "Name": "B2"},
                            "Property": {"PropertyID": 4, "Name": "Oak"},
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_prospect_detail(7))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["data"]["lost_reason"]["name"], "Rented elsewhere")
        self.assertEqual(raw["data"]["contacts"][0]["phone"], "555-0100")
        self.assertEqual(raw["data"]["addresses"][0]["city"], "Akron")
        self.assertEqual([row["stage_name"] for row in raw["data"]["stage_history"]], ["Application", "Guest Card"])
        self.assertEqual(raw["data"]["interested_units"][0]["unit_name"], "B2")
        self.assertEqual(raw["data"]["interested_units"][0]["property_name"], "Oak")
        self.assertEqual(self.transport.auth_calls, 0)

        def missing(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            self.assertEqual(path, "/Prospects")
            return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})

        self.transport.calls.clear()
        self.transport.on_api = missing
        gone = json.loads(self.server.rm_prospect_detail(7))
        self.assertTrue(gone["ok"], gone)
        self.assertFalse(gone["meta"]["found"])
        self.assertEqual(len(self.transport.calls), 1)

        def history_rejected(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Prospects":
                return FakeResponse(
                    200,
                    body=[{"ProspectID": 7, "Name": "Ada Guest", "ProspectStatus": "Prospect"}],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Prospects/7/ProspectStageHistory":
                return FakeResponse(400, text="bad embed")
            if path == "/Prospects/7/InterestedRentals":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            raise AssertionError(path)

        self.transport.on_api = history_rejected
        partial = json.loads(self.server.rm_prospect_detail(7))
        self.assertTrue(partial["ok"], partial)
        self.assertEqual(partial["data"]["name"], "Ada Guest")
        self.assertEqual(partial["data"]["stage_history"], [])
        self.assertIn("ProspectStageHistory", partial["meta"]["warnings"][0])

        def lookups(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("filters", params)
            self.assertNotIn("orderby", params)
            body = {
                "/ProspectStages": [
                    {"ProspectStageID": 2, "Name": "Application", "IsActive": True, "SortOrder": 2},
                    {"ProspectStageID": 1, "Name": "Guest Card", "IsActive": True, "SortOrder": 1},
                    {"ProspectStageID": 9, "Name": "Old", "IsActive": False, "SortOrder": 0},
                ],
                "/ProspectLostReasons": [
                    {"ProspectLostReasonID": 4, "Name": "Rented elsewhere", "ProspectStatus": "Lost", "IsActive": True, "SortOrder": 1}
                ],
                "/LeadSources": [
                    {"ProspectLeadSourceID": 8, "Description": "Apartments.com", "PhoneNumber": "555-0199"},
                    {"ProspectLeadSourceID": 3, "Description": "Retired", "IsActive": False},
                ],
            }[path]
            return FakeResponse(200, body=body, headers={"X-Total-Results": str(len(body))})

        self.transport.calls.clear()
        self.transport.on_api = lookups
        listed = json.loads(self.server.rm_prospect_lookups())
        self.assertTrue(listed["ok"], listed)
        self.assertEqual([row["name"] for row in listed["data"]["stages"]], ["Guest Card", "Application"])
        self.assertEqual(listed["data"]["lead_sources"][0]["name"], "Apartments.com")
        self.assertEqual(listed["data"]["lead_sources"][0]["phone"], "555-0199")
        self.assertEqual(listed["data"]["lost_reasons"][0]["prospect_status"], "Lost")
        self.assertNotIn("Retired", json.dumps(listed["data"]["lead_sources"]))

        self.transport.calls.clear()
        stages = json.loads(self.server.rm_prospect_lookups(which="stages"))
        self.assertTrue(stages["ok"], stages)
        self.assertEqual(
            [call["url"].split("rm.example.test", 1)[-1] for call in self.transport.calls],
            ["/ProspectStages"],
        )

    def test_leases_expiring_window_renewal_status_and_rent(self) -> None:
        leases = [
            {
                "LeaseID": 1,
                "TenantID": 10,
                "UnitID": 100,
                "PropertyID": 4,
                "MoveInDate": "2025-01-01",
                "NoticeDate": "2026-10-15",
                "ExpectedMoveOutDate": "2026-11-30",
                "Tenant": {"TenantID": 10, "Name": "Ada Tenant"},
                "Unit": {"UnitID": 100, "Name": "A1"},
                "LeaseRenewals": [
                    {"LeaseRenewalID": 11, "ParentLeaseID": 1, "StartDate": "2025-12-01", "EndDate": "2026-12-01"}
                ],
            },
            {
                "LeaseID": 2,
                "TenantID": 12,
                "UnitID": 102,
                "PropertyID": 4,
                "ExpectedMoveOutDate": "2026-11-15",
                "Tenant": {"TenantID": 12, "Name": "Far Lease"},
                "Unit": {"UnitID": 102, "Name": "C3"},
                "LeaseRenewals": [
                    {"LeaseRenewalID": 21, "StartDate": "2026-08-01", "EndDate": "2027-08-01"}
                ],
            },
            {
                "LeaseID": 3,
                "TenantID": 13,
                "UnitID": 103,
                "PropertyID": 4,
                "MoveOutDate": "2026-10-01",
                "LeaseRenewals": [
                    {"LeaseRenewalID": 31, "StartDate": "2025-11-01", "EndDate": "2026-11-20"}
                ],
            },
            {
                "LeaseID": 4,
                "TenantID": 14,
                "UnitID": 104,
                "PropertyID": 4,
                "Tenant": {"TenantID": 14, "Name": "Signed Next"},
                "Unit": {"UnitID": 104, "Name": "D4"},
                "LeaseRenewals": [
                    {"LeaseRenewalID": 41, "StartDate": "2025-11-05", "EndDate": "2026-11-20"},
                    {
                        "LeaseRenewalID": 42,
                        "StartDate": "2026-11-20",
                        "EndDate": "2027-11-20",
                        "SignDate": "2026-10-01",
                        "SignableDocumentStatus": "Completed",
                    },
                ],
            },
            {
                "LeaseID": 5,
                "TenantID": 15,
                "UnitID": 105,
                "PropertyID": 4,
                "Tenant": {"TenantID": 15, "Name": "Offer Pending"},
                "Unit": {"UnitID": 105, "Name": "E5"},
                "LeaseRenewals": [
                    {"LeaseRenewalID": 51, "StartDate": "2025-11-10", "EndDate": "2026-11-10"},
                    {
                        "LeaseRenewalID": 52,
                        "StartDate": "2026-11-10",
                        "EndDate": "2027-11-10",
                        "SignableDocumentStatus": "Pending Signature",
                    },
                ],
            },
            {
                "LeaseID": 6,
                "TenantID": 16,
                "UnitID": 106,
                "PropertyID": 4,
                "Tenant": {"Name": "Month to month"},
                "LeaseRenewals": [],
            },
            {
                "LeaseID": 7,
                "TenantID": 17,
                "UnitID": 107,
                "PropertyID": 4,
                "Tenant": {"TenantID": 17, "Name": "Eighty days"},
                "Unit": {"UnitID": 107, "Name": "G7"},
                "LeaseRenewals": [
                    {"LeaseRenewalID": 71, "StartDate": "2026-01-20", "EndDate": "2027-01-20"}
                ],
            },
        ]
        tenants = [
            {
                "TenantID": 10,
                "RecurringCharges": [
                    _charge(1, 1000, "RC"),
                    _charge(2, 200, "RC8"),
                    _charge(3, 50, "RAC"),
                    _charge(4, -25, "RCI"),
                    _charge(5, 300, "RENTIN"),
                    _charge(6, 999, "RC", to_date="2026-01-01"),
                ],
            },
            {"TenantID": 14, "RecurringCharges": [_charge(7, 800, "RC")]},
            {"TenantID": 15, "RecurringCharges": [_charge(9, 100, "RC")]},
            {"TenantID": 17, "RecurringCharges": [_charge(10, 50, "RC")]},
        ]
        units = [
            {
                "UnitID": 100,
                "RecurringCharges": [_charge(1, 999, "RC"), _charge(8, 10, "RC")],
            }
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("orderby", params)
            filters = params.get("filters")
            if path == "/Leases":
                self.assertIn(f"PropertyID,eq,4;ActiveLeaseRenewal.EndDate,ge,{TODAY.isoformat()}", filters)
                self.assertIn("ActiveLeaseRenewal.EndDate,le,", filters)
            elif filters is not None:
                self.assertNotIn(";", filters)
                self.assertEqual(filters, "PropertyID,eq,4")
            if path == "/Leases":
                self.assertEqual(params["embeds"], LEASE_LIST_EMBEDS)
                return FakeResponse(200, body=leases, headers={"X-Total-Results": str(len(leases))})
            if path == "/Tenants":
                self.assertEqual(params["embeds"], _TENANT_RENT_EMBEDS)
                self.assertNotIn("RecurringCharges.ChargeType", params["embeds"])
                return FakeResponse(200, body=tenants, headers={"X-Total-Results": str(len(tenants))})
            if path == "/Units":
                self.assertEqual(params["embeds"], _UNIT_RENT_EMBEDS)
                self.assertNotIn("RecurringCharges.ChargeType", params["embeds"])
                return FakeResponse(200, body=units, headers={"X-Total-Results": str(len(units))})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_leases_expiring(property_id=4))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual([row["lease_id"] for row in raw["data"]], [5, 4, 1, 7])
        self.assertEqual(raw["meta"]["within_days"], 90)
        self.assertEqual(raw["meta"]["open_ended"], 1)
        self.assertEqual(raw["meta"]["charge_mode"], "property_collections")
        first = raw["data"][0]
        self.assertEqual(first["renewal_status"], "renewal_on_file")
        self.assertEqual(first["signable_document_status"], "Pending Signature")
        self.assertIsNone(first["next_renewal_sign_date"])
        self.assertEqual(first["days_until_end"], 6)
        signed = raw["data"][1]
        self.assertEqual(signed["renewal_status"], "signed")
        self.assertEqual(signed["next_renewal_sign_date"], "2026-10-01")
        self.assertEqual(signed["current_rent"], 800.0)
        ada = raw["data"][2]
        self.assertEqual(ada["renewal_status"], "no_renewal")
        self.assertEqual(ada["renewal_count"], 1)
        self.assertEqual(ada["lease_end"], "2026-12-01")
        self.assertEqual(ada["expected_move_out"], "2026-11-30")
        self.assertEqual(ada["notice_date"], "2026-10-15")
        self.assertEqual(ada["tenant_name"], "Ada Tenant")
        self.assertEqual(ada["unit_name"], "A1")
        self.assertEqual(ada["current_rent"], 1210.0)
        self.assertEqual(ada["base_rent_tenant"], 1010.0)
        self.assertEqual(ada["base_rent_subsidized"], 200.0)
        self.assertEqual(ada["surcharges"], 50.0)
        self.assertEqual(ada["concessions"], -25.0)
        self.assertEqual(ada["other_recurring"], 300.0)
        self.assertEqual(ada["days_until_end"], 27)
        self.assertNotIn("rent_complete", ada)
        self.assertNotIn("unresolved_charges", ada)
        self.assertNotIn("rent_complete", raw["meta"])
        self.assertEqual(_TENANT_RENT_EMBEDS, "Leases,RecurringCharges")
        self.assertEqual(_UNIT_RENT_EMBEDS, "RecurringCharges")
        paths = [call["url"].split("rm.example.test", 1)[-1] for call in self.transport.calls]
        self.assertEqual(paths, ["/Leases", "/Tenants", "/Units"])

        self.transport.calls.clear()
        narrower = json.loads(self.server.rm_leases_expiring(property_id=4, within_days=30))
        self.assertEqual([row["lease_id"] for row in narrower["data"]], [5, 4, 1])
        self.assertEqual(narrower["meta"]["within_days"], 30)

    def test_portfolio_expiring_leases_read_charges_per_included_lease(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("orderby", params)
            if path == "/Leases":
                horizon = (TODAY + timedelta(days=90)).isoformat()
                self.assertEqual(
                    params["filters"],
                    f"ActiveLeaseRenewal.EndDate,ge,{TODAY.isoformat()};ActiveLeaseRenewal.EndDate,le,{horizon}",
                )
            else:
                self.assertNotIn("filters", params)
            if path == "/Leases":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "LeaseID": 1,
                            "TenantID": 10,
                            "UnitID": 100,
                            "PropertyID": 4,
                            "Tenant": {"Name": "Ada"},
                            "Unit": {"Name": "A1"},
                            "LeaseRenewals": [
                                {"LeaseRenewalID": 11, "StartDate": "2025-12-01", "EndDate": "2026-12-01"}
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Tenants/10/RecurringCharges":
                self.assertEqual(params["embeds"], "ChargeType")
                return FakeResponse(200, body=[_charge(1, 900, "RC")], headers={"X-Total-Results": "1"})
            if path == "/Units/100/RecurringCharges":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_leases_expiring())
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["meta"]["charge_mode"], "per_lease")
        self.assertEqual(raw["data"][0]["current_rent"], 900.0)
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertNotIn("rent_complete", raw["data"][0])

    def test_property_expiring_joins_charge_types_without_nested_embed(self) -> None:
        catalog = [
            {"ChargeTypeID": 1, "Name": "RC", "Description": "Rent Charge", "GLAccountID": 4311},
            {"ChargeTypeID": 2, "Name": "RC8", "Description": "Subsidized Portion of Rent", "GLAccountID": 4314},
            {"ChargeTypeID": 3, "Name": "RAC", "GLAccountID": 4311},
            {"ChargeTypeID": 4, "Name": "RWD", "GLAccountID": 4311},
            {"ChargeTypeID": 5, "Name": "RCI", "GLAccountID": 5020},
            {"ChargeTypeID": 6, "Name": "RENTIN", "GLAccountID": 4311},
            {"ChargeTypeID": 7, "Name": "STO", "GLAccountID": 4311},
        ]
        tenants = [
            {
                "TenantID": 10,
                "RecurringCharges": [
                    _type_ref(1, 1000, 1),
                    _type_ref(2, 200, 2),
                    _type_ref(3, 50, 3),
                    _type_ref(4, 15, 4),
                    _type_ref(5, -25, 5),
                    _type_ref(6, 300, 6),
                    _type_ref(7, 7, 7),
                    _type_ref(8, 999, 1, to_date="2026-01-01"),
                    {
                        "RecurringChargeID": 9,
                        "ChargeTypeID": 50,
                        "Amount": 4,
                        "FromDate": "2026-01-01",
                        "Comment": "already named",
                        "ChargeType": {"ChargeTypeID": 50, "Name": "RAC"},
                    },
                ],
            }
        ]
        units = [
            {
                "UnitID": 100,
                "RecurringCharges": [
                    _type_ref(1, 999, 1),
                    _type_ref(11, 10, 1),
                ],
            }
        ]

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("orderby", params)
            embeds = params.get("embeds") or ""
            self.assertNotIn("RecurringCharges.ChargeType", embeds)
            if path == "/Leases":
                self.assertTrue(
                    params["filters"].startswith(
                        f"PropertyID,eq,18;ActiveLeaseRenewal.EndDate,ge,{TODAY.isoformat()};"
                    )
                )
                self.assertIn("ActiveLeaseRenewal.EndDate,le,", params["filters"])
                self.assertEqual(params["embeds"], LEASE_LIST_EMBEDS)
                return FakeResponse(200, body=[_lease_row(1, 10, 100)], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                self.assertEqual(params["filters"], "PropertyID,eq,18")
                self.assertEqual(params["embeds"], "Leases,RecurringCharges")
                return FakeResponse(200, body=tenants, headers={"X-Total-Results": "1"})
            if path == "/Units":
                self.assertEqual(params["filters"], "PropertyID,eq,18")
                self.assertEqual(params["embeds"], "RecurringCharges")
                return FakeResponse(200, body=units, headers={"X-Total-Results": "1"})
            if path == "/ChargeTypes":
                self.assertNotIn("filters", params)
                self.assertNotIn("embeds", params)
                return FakeResponse(200, body=catalog, headers={"X-Total-Results": str(len(catalog))})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants", "/Units", "/ChargeTypes"])
        self.assertEqual(self.transport.auth_calls, 0)
        self.assertEqual(raw["meta"]["charge_mode"], "property_collections")
        self.assertNotIn("rent_complete", raw["meta"])
        row = raw["data"][0]
        self.assertEqual(row["lease_id"], 1)
        self.assertEqual(row["renewal_status"], "no_renewal")
        self.assertNotIn("rent_complete", row)
        self.assertNotIn("unresolved_charges", row)
        # Tenant RC 1000 wins over the duplicate unit RC 999. Unit-only RC 10 still counts.
        self.assertEqual(row["current_rent"], 1210.0)
        self.assertEqual(row["base_rent_tenant"], 1010.0)
        self.assertEqual(row["base_rent_subsidized"], 200.0)
        # RAC 50 + RWD 15 + the already-named RAC 4. STO stays other even with GL 4311.
        self.assertEqual(row["surcharges"], 69.0)
        self.assertEqual(row["concessions"], -25.0)
        self.assertEqual(row["other_recurring"], 307.0)
        self.assertEqual(row["recurring_total"], 1561.0)

    def test_unresolved_and_typeless_charges_stay_out_of_rent_totals(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("RecurringCharges.ChargeType", params.get("embeds") or "")
            if path == "/Leases":
                return FakeResponse(
                    200,
                    body=[_lease_row(1, 10, 100), _lease_row(2, 12, 102)],
                    headers={"X-Total-Results": "2"},
                )
            if path == "/Tenants":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "TenantID": 10,
                            "RecurringCharges": [
                                _type_ref(1, 1000, 1),
                                _type_ref(2, 40, 99),
                                _type_ref(3, 12),
                                _type_ref(4, 500, 99, to_date="2026-01-01"),
                            ],
                        },
                        {
                            "TenantID": 12,
                            "RecurringCharges": [_type_ref(8, 800)],
                        },
                    ],
                    headers={"X-Total-Results": "2"},
                )
            if path == "/Units":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            if path == "/ChargeTypes":
                self.assertNotIn("filters", params)
                self.assertNotIn("embeds", params)
                return FakeResponse(
                    200,
                    body=[{"ChargeTypeID": 1, "Name": "RC", "GLAccountID": 4311}],
                    headers={"X-Total-Results": "1"},
                )
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertTrue(raw["ok"], raw)
        self.assertEqual(raw["meta"]["rent_complete"], False)
        self.assertIn("unresolved_charges", raw["meta"]["rent_notice"])
        self.assertIn("not included", raw["meta"]["rent_notice"])
        by_id = {row["lease_id"]: row for row in raw["data"]}
        partial = by_id[1]
        self.assertEqual(partial["current_rent"], 1000.0)
        self.assertEqual(partial["other_recurring"], 12.0)
        self.assertEqual(partial["recurring_total"], 1012.0)
        self.assertEqual(partial["rent_complete"], False)
        self.assertEqual(partial["unresolved_amount"], 40.0)
        self.assertEqual(len(partial["unresolved_charges"]), 1)
        missing = partial["unresolved_charges"][0]
        self.assertEqual(missing["recurring_charge_id"], 2)
        self.assertEqual(missing["charge_type_id"], 99)
        self.assertEqual(missing["amount"], 40.0)
        self.assertIn("99", missing["reason"])
        self.assertEqual(partial["days_until_end"], 27)
        bare = by_id[2]
        self.assertEqual(bare["current_rent"], 0.0)
        self.assertEqual(bare["other_recurring"], 0.0)
        self.assertEqual(bare["recurring_total"], 0.0)
        self.assertEqual(bare["rent_complete"], False)
        self.assertEqual(bare["unresolved_amount"], 800.0)
        self.assertEqual(bare["unresolved_charges"][0]["charge_type_id"], None)
        self.assertIn("no ChargeTypeID", bare["unresolved_charges"][0]["reason"])
        self.assertEqual(self.transport.auth_calls, 0)

        def bare_only(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            self.assertNotIn("RecurringCharges.ChargeType", (kwargs.get("params") or {}).get("embeds") or "")
            if path == "/Leases":
                return FakeResponse(200, body=[_lease_row(2, 12, 102)], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                return FakeResponse(
                    200,
                    body=[{"TenantID": 12, "RecurringCharges": [_type_ref(8, 800)]}],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Units":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = bare_only
        only = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertTrue(only["ok"], only)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants", "/Units"])
        self.assertEqual(only["data"][0]["unresolved_amount"], 800.0)
        self.assertEqual(only["data"][0]["current_rent"], 0.0)
        self.assertEqual(only["meta"]["rent_complete"], False)

    def test_charge_errors_are_not_hidden_as_empty_rent(self) -> None:
        lease = _lease_row(1, 10, 100)

        def leases_only(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Leases":
                return FakeResponse(200, body=[lease], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                return FakeResponse(403, text="Insufficient privileges")
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = leases_only
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        denied = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["type"], "RMForbiddenError")
        self.assertEqual(denied["error"]["status"], 403)
        self.assertIn("Insufficient privileges", denied["error"]["message"])
        self.assertNotIn("data", denied)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants"])
        self.assertEqual(self.transport.auth_calls, 0)

        def units_missing(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Leases":
                return FakeResponse(200, body=[lease], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                self.assertNotIn("RecurringCharges.ChargeType", kwargs["params"]["embeds"])
                return FakeResponse(
                    200,
                    body=[{"TenantID": 10, "RecurringCharges": [_type_ref(1, 1000, 1)]}],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Units":
                return FakeResponse(404, text='{"Message":"KeyNotFoundException"}')
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = units_missing
        missing = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["type"], "RMError")
        self.assertEqual(missing["error"]["status"], 404)
        self.assertIn("KeyNotFoundException", missing["error"]["message"])
        self.assertNotIn("data", missing)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants", "/Units"])

        def catalog_denied(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("RecurringCharges.ChargeType", params.get("embeds") or "")
            if path == "/Leases":
                return FakeResponse(200, body=[lease], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                return FakeResponse(
                    200,
                    body=[{"TenantID": 10, "RecurringCharges": [_type_ref(1, 1000, 1)]}],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Units":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            if path == "/ChargeTypes":
                return FakeResponse(403, text="Insufficient privileges to read charge types")
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = catalog_denied
        catalog = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertFalse(catalog["ok"])
        self.assertEqual(catalog["error"]["type"], "RMForbiddenError")
        self.assertEqual(catalog["error"]["status"], 403)
        self.assertIn("charge types", catalog["error"]["message"])
        self.assertNotIn("data", catalog)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants", "/Units", "/ChargeTypes"])
        self.assertFalse(any(path.endswith("/RecurringCharges") for path in _paths(self.transport)))

        def per_lease_denied(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Leases":
                filters = kwargs["params"]["filters"]
                self.assertNotIn("PropertyID", filters)
                self.assertIn(f"ActiveLeaseRenewal.EndDate,ge,{TODAY.isoformat()}", filters)
                self.assertIn("ActiveLeaseRenewal.EndDate,le,", filters)
                return FakeResponse(200, body=[lease], headers={"X-Total-Results": "1"})
            if path == "/Tenants/10/RecurringCharges":
                self.assertEqual(kwargs["params"]["embeds"], "ChargeType")
                return FakeResponse(403, text="Insufficient privileges")
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = per_lease_denied
        per_lease = json.loads(self.server.rm_leases_expiring())
        self.assertFalse(per_lease["ok"])
        self.assertEqual(per_lease["error"]["status"], 403)
        self.assertEqual(per_lease["error"]["type"], "RMForbiddenError")
        self.assertNotIn("data", per_lease)
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants/10/RecurringCharges"])
        self.assertNotEqual(per_lease["error"]["message"], "")

    def test_truncated_charge_type_catalog_does_not_calculate_rent(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Leases":
                return FakeResponse(200, body=[_lease_row(1, 10, 100)], headers={"X-Total-Results": "1"})
            if path == "/Tenants":
                return FakeResponse(
                    200,
                    body=[{"TenantID": 10, "RecurringCharges": [_type_ref(1, 1000, 1)]}],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Units":
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            if path == "/ChargeTypes":
                return FakeResponse(
                    200,
                    body=[{"ChargeTypeID": 1, "Name": "RC"}],
                    headers={"X-Total-Results": "50"},
                )
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_leases_expiring(property_id=18))
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"]["type"], "RMError")
        self.assertIn("GET /ChargeTypes stopped", raw["error"]["message"])
        self.assertNotIn("data", raw)
        self.assertNotIn("current_rent", json.dumps(raw))
        self.assertEqual(_paths(self.transport), ["/Leases", "/Tenants", "/Units", "/ChargeTypes"])
        self.assertEqual(self.transport.auth_calls, 0)

    def test_lease_renewal_detail_reads_offers_and_one_filter(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs.get("params") or {}
            self.assertNotIn("orderby", params)
            if path == "/Leases":
                self.assertEqual(params["filters"], "LeaseID,eq,9")
                self.assertNotIn(";", params["filters"])
                self.assertEqual(params["embeds"], LEASE_DETAIL_EMBEDS)
                return FakeResponse(
                    200,
                    body=[
                        {
                            "LeaseID": 9,
                            "TenantID": 10,
                            "UnitID": 100,
                            "PropertyID": 4,
                            "Tenant": {"Name": "Ada"},
                            "Unit": {"Name": "A1"},
                            "Property": {"Name": "Oak"},
                            "ActiveLeaseRenewal": {"LeaseRenewalID": 41},
                            "LeaseRenewals": [
                                {
                                    "LeaseRenewalID": 41,
                                    "StartDate": "2025-11-05",
                                    "EndDate": "2026-11-20",
                                    "LeaseTerm": {"LeaseTermID": 1, "Name": "12 Month"},
                                },
                                {
                                    "LeaseRenewalID": 42,
                                    "StartDate": "2026-11-20",
                                    "EndDate": "2027-11-20",
                                    "SignDate": "2026-10-01",
                                },
                            ],
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/LeaseRenewals/41/LeaseRenewalIncreaseOffer":
                return FakeResponse(404, text="missing")
            if path == "/LeaseRenewals/42/LeaseRenewalIncreaseOffer":
                return FakeResponse(
                    200,
                    body={
                        "LeaseRenewalIncreaseOfferID": 7,
                        "Amount": 50,
                        "ChargeIncrease": 25,
                        "ApprovalDate": "2026-10-02",
                        "Comment": "Approved bump",
                    },
                )
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        raw = json.loads(self.server.rm_lease_renewal_detail(lease_id=9, tenant_id=10))
        self.assertTrue(raw["ok"], raw)
        lease = raw["data"]["leases"][0]
        self.assertEqual(lease["renewal_status"], "signed")
        self.assertEqual(lease["property_name"], "Oak")
        self.assertEqual(lease["active_lease_renewal_id"], 41)
        self.assertEqual(lease["renewals"][0]["lease_term"], "12 Month")
        self.assertIsNone(lease["renewals"][0]["increase_offer"])
        self.assertTrue(lease["renewals"][0]["is_current"])
        self.assertEqual(lease["renewals"][1]["increase_offer"]["offer_status"], "approved")
        self.assertEqual(lease["renewals"][1]["increase_offer"]["amount"], 50.0)
        self.assertEqual(self.transport.auth_calls, 0)

        def by_tenant(method: str, url: str, **kwargs: Any) -> FakeResponse:
            path = url.split("rm.example.test", 1)[-1]
            if path == "/Leases":
                self.assertEqual(kwargs["params"]["filters"], "TenantID,eq,10")
                return FakeResponse(200, body=[], headers={"X-Total-Results": "0"})
            raise AssertionError(path)

        self.transport.calls.clear()
        self.transport.on_api = by_tenant
        empty = json.loads(self.server.rm_lease_renewal_detail(tenant_id=10))
        self.assertTrue(empty["ok"], empty)
        self.assertFalse(empty["meta"]["found"])
        missing = json.loads(self.server.rm_lease_renewal_detail())
        self.assertFalse(missing["ok"])

    def test_appointments_tasks_users_and_agenda(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertEqual(_header_token(kwargs.get("headers")), TOKEN)
            path = url.split("rm.example.test", 1)[-1]
            params = kwargs["params"]
            self.assertNotIn("filters", params)
            self.assertNotIn("orderby", params)
            if path == "/Appointments":
                self.assertEqual(params["embeds"], APPOINTMENT_EMBEDS)
                return FakeResponse(
                    200,
                    body=[
                        {
                            "AppointmentID": 1,
                            "Subject": "Showing",
                            "StartDate": "2026-11-04T15:00:00",
                            "EndDate": "2026-11-04T15:30:00",
                            "TenantID": 10,
                            "Tenant": {"TenantID": 10, "Name": "Ada", "PropertyID": 4},
                            "AppointmentUsers": [{"UserID": 5}, {"UserID": 6}],
                        },
                        {
                            "AppointmentID": 2,
                            "Subject": "Tomorrow",
                            "StartDate": "2026-11-05T09:00:00",
                            "AppointmentUsers": [{"UserID": 5}],
                            "Tenant": {"PropertyID": 9, "Name": "Other"},
                        },
                        {
                            "AppointmentID": 3,
                            "Subject": "Prospect tour",
                            "StartDate": "2026-11-04T11:00:00",
                            "ProspectID": 20,
                            "Prospect": {"ProspectID": 20, "Name": "Pat", "PropertyID": 4},
                            "AppointmentUsers": [{"UserID": 8}],
                        },
                        {
                            "AppointmentID": 4,
                            "Subject": "Weekly series",
                            "IsRecurring": True,
                            "StartDate": "2026-01-01T09:00:00",
                            "AppointmentRecurrenceFromDate": "2026-01-01",
                            "AppointmentRecurrenceToDate": "2026-12-31",
                            "AppointmentUsers": [{"UserID": 5}],
                        },
                    ],
                    headers={"X-Total-Results": "4"},
                )
            if path == "/Tasks":
                self.assertEqual(params["embeds"], TASK_EMBEDS)
                return FakeResponse(200, body=_tasks(), headers={"X-Total-Results": "5"})
            if path == "/Users/5/Tasks":
                self.assertEqual(params["embeds"], TASK_EMBEDS)
                return FakeResponse(200, body=_tasks(), headers={"X-Total-Results": "5"})
            if path == "/Users":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "UserID": 5,
                            "Username": "ada",
                            "Firstname": "Ada",
                            "Lastname": "Cole",
                            "Name": "Ada Cole",
                            "Email": "ada@kline.test",
                            "IsActive": True,
                            "IsLeasingAgent": True,
                            "Password": "secret-password",
                            "PartnerToken": "secret-partner-token",
                        },
                        {"UserID": 6, "Username": "bob", "Name": "Bob", "IsActive": False},
                        {"UserID": 7, "Username": "cam", "Name": "Cam"},
                    ],
                    headers={"X-Total-Results": "3"},
                )
            raise AssertionError(path)

        def _tasks() -> list[dict[str, Any]]:
            return [
                {
                    "TaskID": 1,
                    "Subject": "Overdue",
                    "DueDate": "2026-11-01T17:00:00",
                    "IsClosed": False,
                    "TaskUsers": [{"UserID": 5}],
                    "ProspectID": 20,
                    "Prospect": {"Name": "Pat"},
                },
                {"TaskID": 2, "Subject": "Today", "DueDate": "2026-11-04T09:00:00", "IsClosed": False},
                {"TaskID": 3, "Subject": "Later", "DueDate": "2026-12-01T09:00:00", "IsClosed": False},
                {"TaskID": 4, "Subject": "Done", "DueDate": "2026-11-01", "IsClosed": True},
                {"TaskID": 5, "Subject": "Undated", "IsClosed": False},
                {"TaskID": 6, "Subject": "Closed by date", "CloseDate": "2026-11-02", "IsClosed": False},
            ]

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        day = json.loads(self.server.rm_list_appointments(user_id=5))
        self.assertTrue(day["ok"], day)
        self.assertEqual([row["appointment_id"] for row in day["data"]], [1])
        self.assertEqual(day["data"][0]["starts_at"], "2026-11-04T15:00:00")
        self.assertEqual(day["data"][0]["property_id"], 4)
        self.assertEqual(day["data"][0]["tenant_name"], "Ada")
        self.assertEqual(day["meta"]["date_from"], "2026-11-04")
        self.assertNotIn("Weekly series", json.dumps(day["data"]))

        mine = json.loads(self.server.rm_list_appointments(prospect_id=20))
        self.assertEqual([row["subject"] for row in mine["data"]], ["Prospect tour"])
        at_oak = json.loads(self.server.rm_list_appointments(property_id=4))
        self.assertEqual([row["subject"] for row in at_oak["data"]], ["Prospect tour", "Showing"])

        tasks = json.loads(self.server.rm_open_tasks())
        self.assertEqual([row["subject"] for row in tasks["data"]], ["Overdue", "Today", "Later", "Undated"])
        self.assertTrue(tasks["data"][0]["is_overdue"])
        self.assertEqual(tasks["meta"]["source_path"], "/Tasks")
        overdue = json.loads(self.server.rm_open_tasks(overdue_only=True))
        self.assertEqual([row["subject"] for row in overdue["data"]], ["Overdue"])
        assigned = json.loads(self.server.rm_open_tasks(user_id=5, due_to="2026-11-04"))
        self.assertEqual(assigned["meta"]["source_path"], "/Users/5/Tasks")
        self.assertEqual([row["subject"] for row in assigned["data"]], ["Overdue", "Today"])

        users = json.loads(self.server.rm_list_users())
        self.assertEqual([row["user_id"] for row in users["data"]], [5, 7])
        self.assertNotIn("secret-password", json.dumps(users))
        self.assertNotIn("secret-partner-token", json.dumps(users))
        ada = json.loads(self.server.rm_list_users(query="Cole"))
        self.assertEqual(ada["data"][0]["name"], "Ada Cole")
        self.assertTrue(ada["data"][0]["is_leasing_agent"])

        self.transport.calls.clear()
        agenda = json.loads(self.server.rm_staff_agenda(5))
        self.assertTrue(agenda["ok"], agenda)
        self.assertEqual(agenda["data"]["date"], "2026-11-04")
        self.assertEqual([row["subject"] for row in agenda["data"]["appointments"]], ["Showing"])
        self.assertEqual([row["subject"] for row in agenda["data"]["tasks"]], ["Overdue", "Today", "Undated"])
        self.assertEqual(
            [call["url"].split("rm.example.test", 1)[-1] for call in self.transport.calls],
            ["/Appointments", "/Users/5/Tasks"],
        )
        self.assertEqual(self.transport.auth_calls, 0)
        wide = json.loads(self.server.rm_list_appointments(date_from="2026-01-01", date_to="2026-06-01"))
        self.assertFalse(wide["ok"])

    def test_reports_list_parameters_and_run_get_only(self) -> None:
        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertEqual(method, "GET")
            self.assertNotEqual(method, "POST")
            path = url.split("rm.example.test", 1)[-1].split("?", 1)[0]
            params = kwargs.get("params") or {}
            self.assertNotIn("filters", params)
            self.assertNotIn("orderby", params)
            if path == "/Reports":
                self.assertEqual(params.get("embeds"), REPORT_LIST_EMBEDS)
                return FakeResponse(
                    200,
                    body=[
                        {
                            "ReportID": 3,
                            "Name": "Rent Roll",
                            "Description": "Rent by unit",
                            "ReportCategoryID": 2,
                            "ReportCategory": {"ReportCategoryID": 2, "Name": "Rental"},
                        },
                        {"ReportID": 4, "Name": "Owners", "ReportCategory": {"Name": "Ownership"}},
                    ],
                    headers={"X-Total-Results": "2"},
                )
            if path == "/MemorizedReports":
                self.assertEqual(params.get("embeds"), MEMORIZED_LIST_EMBEDS)
                return FakeResponse(
                    200,
                    body=[
                        {
                            "MemorizedReportID": 8,
                            "Name": "My Rent Roll",
                            "ReportID": 3,
                            "IsExcludedFromMenu": False,
                            "Report": {"Description": "Rent by unit", "ReportCategory": {"Name": "Rental"}},
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Reports/3/ReportParameters":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "ReportParameter": {"ReportParameterID": "0", "PropertyName": "PropertyIDs"},
                            "PropertyName": "PropertyIDs",
                            "DisplayName": "Properties",
                            "PromptText": "Pick properties",
                            "DataType": "String",
                            "IsHidden": False,
                            "SortOrder": 1,
                            "DefaultValue": "4",
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/MemorizedReports/8/RunParameters":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "ReportParameterID": "AsOfDate",
                            "DisplayName": "As of",
                            "DataType": "Date",
                            "SortOrder": 2,
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/MemorizedReports/8/MemorizedReportSavedReportParameterValues":
                return FakeResponse(
                    200,
                    body=[
                        {
                            "MemorizedReportSavedReportParameterValueID": 2,
                            "MemorizedReportID": 8,
                            "ReportParameterValue": "4",
                            "IsPrompt": True,
                        }
                    ],
                    headers={"X-Total-Results": "1"},
                )
            if path == "/Reports/3/RunReport":
                self.assertNotIn("filters", params)
                if params.get("getOptions") == "ReturnCSVUrl":
                    return FakeResponse(
                        200,
                        body="https://rm.example.test/Reports/DownloadFile?reportKey=abc",
                        headers={"Content-Type": "application/json"},
                    )
                self.assertNotIn("getOptions", params)
                self.assertEqual(params["parameters[0].Parameter"], "PropertyIDs")
                self.assertEqual(params["parameters[0].Value"], "4")
                self.assertEqual(params["parameters[1].Parameter"], "AsOfDate")
                self.assertEqual(params["parameters[1].Value"], "2026-11-04")
                return FakeResponse(
                    200,
                    body={"Name": "Rent Roll", "Rows": [{"Unit": "A1"}, {"Unit": "A2"}]},
                    headers={"Content-Type": "application/json"},
                )
            if path == "/Reports/DownloadFile":
                self.assertEqual(params.get("reportKey"), "abc")
                return FakeResponse(200, text="Unit,Rent\nA1,1000\n", headers={"Content-Type": "text/csv"})
            if path == "/MemorizedReports/8/RunReport":
                return FakeResponse(200, text="%PDF-1.4\nbinary-body", headers={"Content-Type": "application/pdf"})
            raise AssertionError(path)

        self.make_client()
        self.transport.on_api = api
        self.transport.on_auth = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login"))
        listed = json.loads(self.server.rm_list_reports(query="rent"))
        self.assertTrue(listed["ok"], listed)
        self.assertEqual([row["kind"] for row in listed["data"]], ["report", "memorized"])
        self.assertEqual(listed["data"][0]["report_id"], 3)
        self.assertEqual(listed["data"][0]["category_name"], "Rental")
        self.assertEqual(listed["data"][1]["memorized_report_id"], 8)

        self.transport.calls.clear()
        only = json.loads(self.server.rm_list_reports(kind="memorized"))
        self.assertTrue(only["ok"], only)
        self.assertEqual(
            [call["url"].split("rm.example.test", 1)[-1] for call in self.transport.calls],
            ["/MemorizedReports"],
        )

        params = json.loads(self.server.rm_report_parameters(report_id=3))
        self.assertEqual(params["data"]["parameters"][0]["parameter"], "PropertyIDs")
        self.assertEqual(params["data"]["parameters"][0]["prompt"], "Pick properties")
        saved = json.loads(self.server.rm_report_parameters(memorized_report_id=8))
        self.assertEqual(saved["data"]["parameters"][0]["parameter"], "AsOfDate")
        self.assertEqual(saved["data"]["saved_values"][0]["value"], "4")
        self.assertIn("not matched", saved["data"]["saved_values_note"])

        self.transport.calls.clear()
        both = json.loads(self.server.rm_report_parameters(report_id=3, memorized_report_id=8))
        self.assertFalse(both["ok"])
        self.assertEqual(self.transport.calls, [])
        bad_name = json.loads(
            self.server.rm_run_report(report_id=3, parameters_json='{"Property IDs": "4"}')
        )
        self.assertFalse(bad_name["ok"])
        self.assertEqual(self.transport.calls, [])

        ran = json.loads(
            self.server.rm_run_report(
                report_id=3,
                parameters_json='{"PropertyIDs": "4", "AsOfDate": "2026-11-04"}',
            )
        )
        self.assertTrue(ran["ok"], ran)
        self.assertEqual(ran["data"]["output_kind"], "rows")
        self.assertEqual(ran["data"]["header"]["Name"], "Rent Roll")
        self.assertEqual(ran["data"]["rows"][0]["Unit"], "A1")
        self.assertIsNone(ran["meta"]["get_option"])
        self.assertFalse(ran["meta"]["followed_download"])
        self.assertTrue(all(call["method"] == "GET" for call in self.transport.calls))

        followed = json.loads(self.server.rm_run_report(report_id=3, output="csv", parameters_json="{}"))
        self.assertTrue(followed["ok"], followed)
        self.assertEqual(followed["data"]["output_kind"], "text")
        self.assertIn("A1,1000", followed["data"]["text"])
        self.assertTrue(followed["meta"]["followed_download"])
        self.assertEqual(followed["meta"]["get_option"], "ReturnCSVUrl")

        pdf = json.loads(self.server.rm_run_report(memorized_report_id=8, output="pdf"))
        self.assertEqual(pdf["data"]["output_kind"], "file")
        self.assertIn("not included", pdf["data"]["summary"])
        self.assertNotIn("binary-body", json.dumps(pdf))
        self.assertEqual(pdf["meta"]["endpoint"], "/MemorizedReports/8/RunReport")
        self.assertEqual(self.transport.auth_calls, 0)

        def external(method: str, url: str, **kwargs: Any) -> FakeResponse:
            self.assertNotIn("evil.example", url)
            return FakeResponse(
                200,
                body="https://evil.example/Reports/DownloadFile?reportKey=abc",
                headers={"Content-Type": "application/json"},
            )

        self.transport.calls.clear()
        self.transport.on_api = external
        skipped = json.loads(self.server.rm_run_report(report_id=3))
        self.assertEqual(skipped["data"]["output_kind"], "url")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertFalse(skipped["meta"]["followed_download"])

    def test_report_output_is_capped(self) -> None:
        rows, truncated = summarize_report_body(
            json.dumps([{"n": index} for index in range(81)]).encode(),
            "application/json",
        )
        self.assertTrue(truncated)
        self.assertEqual(rows["row_count"], 80)
        text, text_cut = summarize_report_body(b"x" * 30000, "text/plain")
        self.assertTrue(text_cut)
        self.assertEqual(len(text["text"]), 24000)
        pdf, pdf_cut = summarize_report_body(b"%PDF-1.4 leftover", "application/pdf")
        self.assertFalse(pdf_cut)
        self.assertEqual(pdf["output_kind"], "file")
        self.assertNotIn("leftover", pdf["summary"])

        def api(method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                200,
                body=[{"n": index} for index in range(81)],
                headers={"Content-Type": "application/json"},
            )

        self.make_client()
        self.transport.on_api = api
        raw = json.loads(self.server.rm_run_report(report_id=3))
        self.assertTrue(raw["meta"]["truncated"])
        self.assertIn("CUT OFF", raw["meta"]["notice"])
        self.assertEqual(len(raw["data"]["rows"]), 80)
