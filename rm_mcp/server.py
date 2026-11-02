"""Thin Rent Manager MCP server (stdio) — Phase A reads + Phase B guarded writes."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import (
    RMClient,
    UDF_ASKING_RENT,
    UDF_LIST_FOR_LEASE,
    UDF_LIST_ON_TENANT_TURNER,
)
from .normalize import (
    normalize_address,
    normalize_charge,
    normalize_lease,
    normalize_payment,
    normalize_property,
    normalize_prospect,
    normalize_tenant,
    normalize_udf_field,
    normalize_unit,
    normalize_vendor,
    normalize_work_order,
)
from .writes import (
    ALLOWLISTED_UDF_FIELD_IDS,
    NOTE_ENTITY_TYPES,
    build_add_note_plan,
    build_attach_bill_file_plan,
    build_attach_check_file_plan,
    build_attach_credit_card_file_plan,
    build_attach_vendor_file_plan,
    build_attach_work_order_photo_plan,
    build_create_check_plan,
    build_create_credit_card_transaction_plan,
    build_create_task_plan,
    build_create_vendor_bill_plan,
    build_create_work_order_plan,
    build_link_work_order_plan,
    build_update_check_plan,
    build_update_credit_card_transaction_plan,
    build_update_unit_udf_plan,
    build_update_vendor_bill_plan,
    build_update_work_order_plan,
    build_upload_history_attachment_plan,
    build_upload_unit_image_plan,
    guarded_write,
    load_write_matrix,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("rm-mcp")

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"

mcp = MCPServer(
    name="rent-manager",
    instructions=(
        "Rent Manager domain tools for Kline Properties. "
        "Prefer these named tools over raw RM endpoint calls. "
        "Strategy: coarse RM filters + Python join/filter/sort. "
        "Reads are primary. Phase B write tools default to dry_run=true; live writes require approved_by."
    ),
)

_client: RMClient | None = None
_udf_cache: list[dict[str, Any]] | None = None
_wo_status_cache: dict[int, str] | None = None
_wo_category_cache: dict[int, str] | None = None
_wo_priority_cache: dict[int, str] | None = None


def client() -> RMClient:
    global _client
    if _client is None:
        _client = RMClient()
    return _client


def _ok(data: Any, **meta: Any) -> str:
    payload: dict[str, Any] = {"ok": True, "data": data}
    if meta:
        payload["meta"] = meta
    return json.dumps(payload, default=str)


def _err(exc: Exception) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "status": getattr(exc, "status", None),
                "body": getattr(exc, "body", None),
            },
        },
        default=str,
    )


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(int(n), hi))


def _is_active_lease(raw: dict[str, Any]) -> bool:
    return not raw.get("MoveOutDate") and not raw.get("DepartureDate")


def _address_index(unit_names: list[str] | None = None) -> dict[str, dict[str, Any]]:
    c = client()
    rows = c.get_all("/Units/Addresses", embeds="Unit", pagesize=500)
    idx: dict[str, dict[str, Any]] = {}
    name_set = set(unit_names) if unit_names is not None else None
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit = row.get("Unit") or {}
        name = None
        unit_id = None
        if isinstance(unit, dict):
            name = unit.get("Name")
            unit_id = unit.get("UnitID")
        name = name or row.get("UnitName")
        if not name and unit_id is None:
            continue
        name_s = str(name) if name else None
        if name_set is not None and (name_s is None or name_s not in name_set):
            continue
        dto = normalize_address(row) or {}
        if name_s:
            idx[name_s] = dto
        if unit_id is not None:
            idx[f"id:{unit_id}"] = dto
    return idx


def _attach_address(unit_raw: dict[str, Any], addr_idx: dict[str, dict[str, Any]] | None):
    if not addr_idx:
        return None
    name = str(unit_raw.get("Name")) if unit_raw.get("Name") is not None else None
    uid = unit_raw.get("UnitID")
    return (addr_idx.get(name) if name else None) or (
        addr_idx.get(f"id:{uid}") if uid is not None else None
    )


def _load_udf_catalog(force: bool = False) -> list[dict[str, Any]]:
    global _udf_cache
    if _udf_cache is not None and not force:
        return _udf_cache
    rows = client().get_all("/UserDefinedFields", pagesize=200)
    _udf_cache = [normalize_udf_field(r) for r in rows if isinstance(r, dict)]
    return _udf_cache


def _wo_lookups():
    global _wo_status_cache, _wo_category_cache, _wo_priority_cache
    c = client()
    if _wo_status_cache is None:
        rows = c.get_all("/ServiceManagerStatuses", pagesize=100)
        _wo_status_cache = {
            int(r["ServiceManagerStatusID"]): r.get("Name") or str(r.get("ServiceManagerStatusID"))
            for r in rows
            if isinstance(r, dict) and r.get("ServiceManagerStatusID") is not None
        }
    if _wo_category_cache is None:
        rows = c.get_all("/ServiceManagerCategories", pagesize=100)
        _wo_category_cache = {
            int(r["ServiceManagerCategoryID"]): r.get("Name") or str(r.get("ServiceManagerCategoryID"))
            for r in rows
            if isinstance(r, dict) and r.get("ServiceManagerCategoryID") is not None
        }
    if _wo_priority_cache is None:
        rows = c.get_all("/ServiceManagerPriorities", pagesize=50)
        _wo_priority_cache = {
            int(r["ServiceManagerPriorityID"]): r.get("Name") or str(r.get("ServiceManagerPriorityID"))
            for r in rows
            if isinstance(r, dict) and r.get("ServiceManagerPriorityID") is not None
        }
    return _wo_status_cache, _wo_category_cache, _wo_priority_cache


def _enrich_work_order(dto: dict[str, Any]) -> dict[str, Any]:
    statuses, categories, priorities = _wo_lookups()
    if not dto.get("status_name") and dto.get("status_id") is not None:
        try:
            dto["status_name"] = statuses.get(int(dto["status_id"]))
        except (TypeError, ValueError):
            pass
    if dto.get("category_id") is not None:
        try:
            dto["category_name"] = categories.get(int(dto["category_id"]))
        except (TypeError, ValueError):
            dto["category_name"] = None
    else:
        dto["category_name"] = None
    if dto.get("priority_id") is not None:
        try:
            dto["priority_name"] = priorities.get(int(dto["priority_id"]))
        except (TypeError, ValueError):
            dto["priority_name"] = None
    else:
        dto["priority_name"] = None
    return dto


def _pick_active_lease(leases: list[Any]) -> dict[str, Any] | None:
    active = [x for x in leases if isinstance(x, dict) and _is_active_lease(x)]
    if active:
        return active[0]
    if leases and isinstance(leases[0], dict):
        return leases[0]
    return None


def _tenant_from_row(raw: dict[str, Any]) -> dict[str, Any]:
    dto = normalize_tenant(raw)
    leases = raw.get("Leases") or []
    if isinstance(leases, list):
        pick = _pick_active_lease(leases)
        if pick:
            dto["unit_id"] = pick.get("UnitID") or dto.get("unit_id")
            if not dto.get("move_in_date"):
                mid = pick.get("MoveInDate") or pick.get("ArrivalDate")
                dto["move_in_date"] = (str(mid)[:10] if mid else None)
            dto["active_lease"] = normalize_lease(pick)
            dto["leases"] = [normalize_lease(x) for x in leases if isinstance(x, dict)]
        else:
            dto["active_lease"] = None
            dto["leases"] = []
    return dto


def _digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


@mcp.tool()
def rm_auth_status() -> str:
    """Check Rent Manager auth/token health without fetching portfolio data."""
    try:
        return _ok(client().auth_status())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_find_property(query: str, active_only: bool = True, limit: int = 20) -> str:
    """Find properties by name contains-match. Returns normalized property DTOs.

    Use rm_get_property when you already have property_id.
    """
    try:
        q = (query or "").strip()
        if not q:
            return _err(ValueError("query is required"))
        filters = f"Name,ct,{q}"
        if active_only:
            filters = f"{filters};IsActive,eq,true"
        rows = client().get_all(
            "/Properties",
            filters=filters,
            fields="PropertyID,Name,ShortName,IsActive",
            pagesize=100,
        )
        data = [normalize_property(r) for r in rows if isinstance(r, dict)]
        data = data[: _clamp(limit, 1, 100)]
        return _ok(data, count=len(data), query=q)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_get_property(property_id: int) -> str:
    """Get one property by PropertyID. Returns normalized property DTO.

    Prefer this when you already have property_id. Use rm_find_property for name search.
    """
    try:
        pid = int(property_id)
        rows = client().get_all(
            "/Properties",
            filters=f"PropertyID,eq,{pid}",
            fields="PropertyID,Name,ShortName,IsActive",
            pagesize=5,
        )
        if not rows:
            return _ok(None, found=False, property_id=pid)
        return _ok(normalize_property(rows[0]), found=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_for_lease(
    property_id: int | None = None,
    include_addresses: bool = True,
    limit: int = 200,
) -> str:
    """List units marked List For Lease (UDF 731 = Yes) on active properties.

    Uses /Units with embeds=UserDefinedValues. Never uses /Units/search.
    """
    try:
        filters = (
            f"UserDefinedValues.UserDefinedFieldID,eq,{UDF_LIST_FOR_LEASE};"
            "UserDefinedValues.Value,eq,Yes;Property.IsActive,eq,true"
        )
        if property_id is not None:
            filters = f"{filters};PropertyID,eq,{int(property_id)}"
        rows = client().get_all(
            "/Units",
            filters=filters,
            embeds="UserDefinedValues",
            pagesize=200,
        )
        names = [str(r.get("Name")) for r in rows if isinstance(r, dict) and r.get("Name")]
        addr_idx = _address_index(names) if include_addresses and names else {}
        units = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            addr = _attach_address(r, addr_idx) if include_addresses else None
            units.append(normalize_unit(r, address=addr))
        units = units[: _clamp(limit, 1, 500)]
        return _ok(
            units,
            count=len(units),
            property_id=property_id,
            udf_list_for_lease=UDF_LIST_FOR_LEASE,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_unit_detail(unit_id: int | None = None, unit_name: str | None = None) -> str:
    """Get one unit by UnitID or exact unit Name, with UDFs and address when available."""
    try:
        if unit_id is None and not unit_name:
            return _err(ValueError("Provide unit_id or unit_name"))
        c = client()
        if unit_id is not None:
            filters = f"UnitID,eq,{int(unit_id)}"
        else:
            filters = f"Name,eq,{str(unit_name).strip()}"
        rows = c.get_all(
            "/Units",
            filters=filters,
            embeds="UserDefinedValues",
            pagesize=50,
        )
        if not rows:
            return _ok(None, found=False)
        raw = rows[0]
        name = str(raw.get("Name") or "")
        addr = None
        if name:
            addr_rows = c.get_all(
                "/Units/Addresses",
                filters=f"Unit.Name,eq,{name}",
                embeds="Unit",
                pagesize=20,
            )
            if addr_rows and isinstance(addr_rows[0], dict):
                addr = normalize_address(addr_rows[0])
        return _ok(normalize_unit(raw, address=addr), found=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_find_unit(
    query: str | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
    limit: int = 25,
) -> str:
    """Find units by partial name and/or property_id/unit_id. Returns normalized unit DTOs."""
    try:
        if unit_id is None and not query and property_id is None:
            return _err(ValueError("Provide query, property_id, and/or unit_id"))
        filters_parts: list[str] = []
        if unit_id is not None:
            filters_parts.append(f"UnitID,eq,{int(unit_id)}")
        if property_id is not None:
            filters_parts.append(f"PropertyID,eq,{int(property_id)}")
        filters = ";".join(filters_parts) if filters_parts else None
        rows = client().get_all(
            "/Units",
            filters=filters,
            embeds="UserDefinedValues",
            pagesize=200,
            max_pages=20 if property_id is not None or unit_id is not None else 5,
        )
        q = (query or "").strip().lower()
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = str(r.get("Name") or "")
            if q and q not in name.lower() and q not in str(r.get("UnitID") or ""):
                continue
            out.append(normalize_unit(r))
            if len(out) >= _clamp(limit, 1, 100):
                break
        return _ok(out, count=len(out), query=query, property_id=property_id, unit_id=unit_id)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_occupancy(property_id: int) -> str:
    """Occupancy summary for a property: active leases vs units, vacant list."""
    try:
        pid = int(property_id)
        c = client()
        units = c.get_all(
            "/Units",
            filters=f"PropertyID,eq,{pid}",
            fields="UnitID,Name,PropertyID",
            pagesize=200,
        )
        leases = c.get_all(
            "/Leases",
            filters=f"PropertyID,eq,{pid}",
            pagesize=200,
        )
        unit_map = {}
        for u in units:
            if not isinstance(u, dict):
                continue
            uid = u.get("UnitID")
            unit_map[uid] = u.get("Name") or f"Unit {uid}"

        active = [
            normalize_lease(l)
            for l in leases
            if isinstance(l, dict) and _is_active_lease(l)
        ]
        occupied_ids = {l["unit_id"] for l in active if l.get("unit_id") is not None}
        vacant = [
            {"unit_id": uid, "name": name}
            for uid, name in sorted(unit_map.items(), key=lambda kv: str(kv[1]))
            if uid not in occupied_ids
        ]

        tenant_ids = sorted({l["tenant_id"] for l in active if l.get("tenant_id") is not None})
        name_map: dict[Any, str] = {}
        if tenant_ids:
            id_list = ",".join(str(t) for t in tenant_ids)
            tenants = c.get_all(
                "/Tenants",
                filters=f"TenantID,in,({id_list})",
                fields="TenantID,Name,Status",
                pagesize=200,
            )
            name_map = {
                t.get("TenantID"): t.get("Name") or "?"
                for t in tenants
                if isinstance(t, dict)
            }

        occupied = []
        for l in sorted(active, key=lambda x: str(unit_map.get(x.get("unit_id"), ""))):
            occupied.append(
                {
                    **l,
                    "unit_name": unit_map.get(l.get("unit_id")),
                    "tenant_name": name_map.get(l.get("tenant_id")),
                }
            )

        total = len(unit_map)
        occ_n = len(occupied_ids)
        summary = {
            "property_id": pid,
            "total_units": total,
            "occupied_units": occ_n,
            "vacant_units": len(vacant),
            "occupancy_pct": round(100.0 * occ_n / total, 1) if total else 0.0,
            "occupied": occupied,
            "vacant": vacant,
        }
        return _ok(summary)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_current_tenants(property_id: int, limit: int = 200) -> str:
    """List current tenants for a property."""
    try:
        pid = int(property_id)
        rows = client().get_all(
            "/Tenants",
            filters=f"PropertyID,eq,{pid};Status,eq,Current",
            embeds="Leases",
            pagesize=200,
        )
        data = [_tenant_from_row(r) for r in rows if isinstance(r, dict)]
        data = data[: _clamp(limit, 1, 500)]
        return _ok(data, count=len(data), property_id=pid)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_find_tenant(
    query: str | None = None,
    property_id: int | None = None,
    status: str = "Current",
    tenant_id: int | None = None,
    limit: int = 25,
) -> str:
    """Find tenants by name/email/phone fragment, property, status, or tenant_id.

    Coarse RM filters + Python matching for phone/email fragments.
    status: Current|Past|Prospect|Any
    """
    try:
        if tenant_id is None and not query and property_id is None:
            return _err(ValueError("Provide query, property_id, and/or tenant_id"))
        filters_parts: list[str] = []
        if tenant_id is not None:
            filters_parts.append(f"TenantID,eq,{int(tenant_id)}")
        if property_id is not None:
            filters_parts.append(f"PropertyID,eq,{int(property_id)}")
        st = (status or "Current").strip()
        if st and st.lower() != "any":
            filters_parts.append(f"Status,eq,{st}")
        filters = ";".join(filters_parts) if filters_parts else None

        # Name contains only when no tenant_id and query looks like a name
        q = (query or "").strip()
        use_name_filter = bool(q) and tenant_id is None and "@" not in q and not _digits(q)
        if use_name_filter:
            name_filter = f"Name,ct,{q}"
            filters = f"{filters};{name_filter}" if filters else name_filter

        rows = client().get_all(
            "/Tenants",
            filters=filters,
            embeds="Leases,Contacts",
            pagesize=100,
            max_pages=10,
        )
        q_lower = q.lower()
        q_digits = _digits(q)
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            dto = _tenant_from_row(r)
            if q and not use_name_filter:
                hay = " ".join(
                    str(x or "")
                    for x in [
                        dto.get("name"),
                        dto.get("email"),
                        dto.get("phone"),
                        dto.get("first_name"),
                        dto.get("last_name"),
                        json.dumps(dto.get("contacts") or []),
                    ]
                ).lower()
                if q_lower not in hay and not (q_digits and q_digits in _digits(hay)):
                    continue
            out.append(dto)
            if len(out) >= _clamp(limit, 1, 100):
                break
        return _ok(
            out,
            count=len(out),
            query=query,
            property_id=property_id,
            status=status,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_tenant_detail(tenant_id: int) -> str:
    """Get one tenant by TenantID with contacts and leases (active lease highlighted)."""
    try:
        tid = int(tenant_id)
        rows = client().get_all(
            "/Tenants",
            filters=f"TenantID,eq,{tid}",
            embeds="Leases,Contacts",
            pagesize=5,
        )
        if not rows:
            return _ok(None, found=False, tenant_id=tid)
        return _ok(_tenant_from_row(rows[0]), found=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_lease_detail(lease_id: int) -> str:
    """Get one lease by LeaseID. Returns normalized lease DTO with is_active flag."""
    try:
        lid = int(lease_id)
        rows = client().get_all(
            "/Leases",
            filters=f"LeaseID,eq,{lid}",
            pagesize=5,
        )
        if not rows:
            return _ok(None, found=False, lease_id=lid)
        return _ok(normalize_lease(rows[0]), found=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_leases(
    property_id: int | None = None,
    unit_id: int | None = None,
    tenant_id: int | None = None,
    active_only: bool = True,
    ending_within_days: int | None = None,
    limit: int = 200,
) -> str:
    """List leases with optional property/unit/tenant filters.

    active_only uses Python rule: no MoveOutDate and no DepartureDate.
    ending_within_days is best-effort on departure/move-out dates when present.
    """
    try:
        filters_parts: list[str] = []
        if property_id is not None:
            filters_parts.append(f"PropertyID,eq,{int(property_id)}")
        if unit_id is not None:
            filters_parts.append(f"UnitID,eq,{int(unit_id)}")
        if tenant_id is not None:
            filters_parts.append(f"TenantID,eq,{int(tenant_id)}")
        if not filters_parts:
            return _err(ValueError("Provide property_id, unit_id, and/or tenant_id"))
        rows = client().get_all(
            "/Leases",
            filters=";".join(filters_parts),
            pagesize=200,
        )
        today = date.today()
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if active_only and not _is_active_lease(r):
                continue
            dto = normalize_lease(r)
            if ending_within_days is not None:
                end = dto.get("departure_date") or dto.get("move_out_date")
                if not end:
                    continue
                try:
                    end_d = date.fromisoformat(str(end)[:10])
                except ValueError:
                    continue
                delta = (end_d - today).days
                if delta < 0 or delta > int(ending_within_days):
                    continue
                dto["days_until_end"] = delta
            out.append(dto)
            if len(out) >= _clamp(limit, 1, 500):
                break
        return _ok(
            out,
            count=len(out),
            property_id=property_id,
            unit_id=unit_id,
            tenant_id=tenant_id,
            active_only=active_only,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_unit_udfs(unit_id: int | None = None, unit_name: str | None = None) -> str:
    """Get all UDFs for one unit, plus known leasing fields (731/732/710)."""
    try:
        if unit_id is None and not unit_name:
            return _err(ValueError("Provide unit_id or unit_name"))
        detail = json.loads(rm_unit_detail(unit_id=unit_id, unit_name=unit_name))
        if not detail.get("ok"):
            return json.dumps(detail)
        unit = detail.get("data")
        if not unit:
            return _ok(None, found=False)
        return _ok(
            {
                "unit_id": unit.get("unit_id"),
                "name": unit.get("name"),
                "property_id": unit.get("property_id"),
                "list_for_lease": unit.get("list_for_lease"),
                "asking_rent": unit.get("asking_rent"),
                "list_on_tenant_turner": unit.get("list_on_tenant_turner"),
                "udfs": unit.get("udfs"),
            },
            found=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_vacancy_board(
    property_id: int | None = None,
    include_addresses: bool = True,
    limit: int = 300,
) -> str:
    """Leasing board: vacant and/or list-for-lease units with asking rent and completeness flags.

    Compound tool: units + leases + UDFs + addresses joined in Python.
    """
    try:
        c = client()
        filters = "Property.IsActive,eq,true"
        if property_id is not None:
            filters = f"{filters};PropertyID,eq,{int(property_id)}"
        units = c.get_all(
            "/Units",
            filters=filters,
            embeds="UserDefinedValues",
            pagesize=200,
            max_pages=30,
        )
        # leases for occupancy — property scoped when possible
        if property_id is not None:
            leases = c.get_all("/Leases", filters=f"PropertyID,eq,{int(property_id)}", pagesize=200)
        else:
            # portfolio-wide is heavy; still do it but cap pages
            leases = c.get_all("/Leases", pagesize=500, max_pages=20)

        occupied_unit_ids = {
            l.get("UnitID")
            for l in leases
            if isinstance(l, dict) and _is_active_lease(l) and l.get("UnitID") is not None
        }

        names = [str(u.get("Name")) for u in units if isinstance(u, dict) and u.get("Name")]
        addr_idx = _address_index(names) if include_addresses and names else {}

        board = []
        for u in units:
            if not isinstance(u, dict):
                continue
            uid = u.get("UnitID")
            is_vacant = uid not in occupied_unit_ids
            dto = normalize_unit(u, address=_attach_address(u, addr_idx) if include_addresses else None)
            list_for_lease = bool(dto.get("list_for_lease"))
            if not is_vacant and not list_for_lease:
                continue
            missing = []
            if list_for_lease and dto.get("asking_rent") is None:
                missing.append("asking_rent")
            if list_for_lease and not (dto.get("address") or {}).get("full"):
                missing.append("address")
            if list_for_lease and dto.get("list_on_tenant_turner") is None:
                missing.append("tenant_turner_flag")
            board.append(
                {
                    **dto,
                    "is_vacant": is_vacant,
                    "is_list_for_lease": list_for_lease,
                    "missing_fields": missing,
                    "listing_complete": list_for_lease and not missing,
                }
            )

        board.sort(
            key=lambda x: (
                0 if x.get("is_list_for_lease") else 1,
                0 if x.get("is_vacant") else 1,
                str(x.get("property_id")),
                str(x.get("name") or ""),
            )
        )
        board = board[: _clamp(limit, 1, 1000)]
        summary = {
            "total": len(board),
            "vacant": sum(1 for x in board if x.get("is_vacant")),
            "list_for_lease": sum(1 for x in board if x.get("is_list_for_lease")),
            "listing_complete": sum(1 for x in board if x.get("listing_complete")),
            "missing_asking_rent": sum(1 for x in board if "asking_rent" in (x.get("missing_fields") or [])),
        }
        return _ok(board, count=len(board), summary=summary, property_id=property_id)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_prospects(
    property_id: int | None = None,
    query: str | None = None,
    limit: int = 50,
) -> str:
    """List prospects, optionally filtered by property_id and/or name query."""
    try:
        filters_parts: list[str] = []
        if property_id is not None:
            filters_parts.append(f"PropertyID,eq,{int(property_id)}")
        q = (query or "").strip()
        if q:
            filters_parts.append(f"Name,ct,{q}")
        filters = ";".join(filters_parts) if filters_parts else None
        rows = client().get_all(
            "/Prospects",
            filters=filters,
            pagesize=100,
            max_pages=10 if filters else 3,
        )
        data = [normalize_prospect(r) for r in rows if isinstance(r, dict)]
        data = data[: _clamp(limit, 1, 200)]
        return _ok(data, count=len(data), property_id=property_id, query=query)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_work_orders(
    property_id: int | None = None,
    unit_id: int | None = None,
    tenant_id: int | None = None,
    open_only: bool = True,
    min_age_days: float | None = None,
    limit: int = 50,
) -> str:
    """List service manager work orders (issues).

    Uses proven embeds Status,Units,Properties,Tenants,Vendor. Python filters by
    property/unit/tenant/age. open_only -> IsClosed,eq,false when possible.
    """
    try:
        filters = "IsClosed,eq,false" if open_only else None
        # Pull a bounded set; join/filter in Python for property/unit
        rows = client().get_all(
            "/ServiceManagerIssues",
            filters=filters,
            embeds="Status,Units,Properties,Tenants,Vendor,AssignedToUser",
            pagesize=100,
            max_pages=5,
        )
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            dto = _enrich_work_order(normalize_work_order(r))
            if property_id is not None and dto.get("property_id") != int(property_id):
                # also check embedded lists if property_id missing on dto
                if dto.get("property_id") is not None:
                    continue
                props = r.get("Properties") or []
                if not any(isinstance(p, dict) and p.get("PropertyID") == int(property_id) for p in props):
                    continue
                dto["property_id"] = int(property_id)
            if unit_id is not None and dto.get("unit_id") != int(unit_id):
                units = r.get("Units") or []
                if not any(isinstance(u, dict) and u.get("UnitID") == int(unit_id) for u in units):
                    continue
                dto["unit_id"] = int(unit_id)
            if tenant_id is not None and dto.get("tenant_id") != int(tenant_id):
                tens = r.get("Tenants") or []
                if not any(isinstance(t, dict) and t.get("TenantID") == int(tenant_id) for t in tens):
                    continue
                dto["tenant_id"] = int(tenant_id)
            if min_age_days is not None:
                age = dto.get("age_days")
                if age is None or float(age) < float(min_age_days):
                    continue
            out.append(dto)
            if len(out) >= _clamp(limit, 1, 200):
                break
        # sort oldest first when age present
        out.sort(key=lambda x: (-(x.get("age_days") or 0), str(x.get("work_order_id"))))
        return _ok(
            out,
            count=len(out),
            open_only=open_only,
            property_id=property_id,
            unit_id=unit_id,
            tenant_id=tenant_id,
            note="RM work orders often lack bare PropertyID; embeds used. Create/update use collection POST upsert.",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_work_order_detail(work_order_id: int) -> str:
    """Get one service manager work order by ID, with status/unit/property/tenant embeds when available."""
    try:
        wid = int(work_order_id)
        c = client()
        # direct get
        r = c.request(
            "GET",
            f"/ServiceManagerIssues/{wid}",
            params={"embeds": "Status,Units,Properties,Tenants,Vendor,AssignedToUser"},
        )
        raw = r.json() if r.content else None
        if not raw:
            return _ok(None, found=False, work_order_id=wid)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return _ok(None, found=False, work_order_id=wid)
        dto = _enrich_work_order(normalize_work_order(raw))
        return _ok(dto, found=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_vendors(query: str | None = None, limit: int = 50) -> str:
    """List vendors. Optional name contains filter (Python-side if RM filter flaky)."""
    try:
        q = (query or "").strip()
        filters = f"Name,ct,{q}" if q else None
        try:
            rows = client().get_all("/Vendors", filters=filters, pagesize=100, max_pages=5)
        except Exception:
            rows = client().get_all("/Vendors", pagesize=100, max_pages=5)
            if q:
                ql = q.lower()
                rows = [r for r in rows if isinstance(r, dict) and ql in str(r.get("Name") or "").lower()]
        data = [normalize_vendor(r) for r in rows if isinstance(r, dict)]
        data = data[: _clamp(limit, 1, 200)]
        return _ok(data, count=len(data), query=query)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_open_charges(
    property_id: int | None = None,
    tenant_id: int | None = None,
    limit: int = 100,
) -> str:
    """List open charges (IsFullyAllocated=false). Filter by tenant AccountID and/or property in Python/RM."""
    try:
        filters_parts = ["IsFullyAllocated,eq,false"]
        if tenant_id is not None:
            filters_parts.append(f"AccountID,eq,{int(tenant_id)}")
        if property_id is not None:
            filters_parts.append(f"PropertyID,eq,{int(property_id)}")
        rows = client().get_all(
            "/Charges",
            filters=";".join(filters_parts),
            pagesize=100,
            max_pages=10 if tenant_id or property_id else 3,
        )
        data = [normalize_charge(r) for r in rows if isinstance(r, dict)]
        # newest first
        data.sort(key=lambda x: str(x.get("transaction_date") or ""), reverse=True)
        data = data[: _clamp(limit, 1, 500)]
        total_balance = round(sum(float(x.get("balance") or 0) for x in data), 2)
        return _ok(
            data,
            count=len(data),
            total_balance=total_balance,
            property_id=property_id,
            tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_tenant_balance(tenant_id: int, include_charges: bool = True, limit_charges: int = 50) -> str:
    """Tenant balance from open charges (AccountID=TenantID). Optional open charge lines."""
    try:
        tid = int(tenant_id)
        # tenant header
        trows = client().get_all(
            "/Tenants",
            filters=f"TenantID,eq,{tid}",
            embeds="Leases,Contacts",
            pagesize=5,
        )
        tenant = _tenant_from_row(trows[0]) if trows else {"tenant_id": tid}
        rows = client().get_all(
            "/Charges",
            filters=f"AccountID,eq,{tid};IsFullyAllocated,eq,false",
            pagesize=100,
            max_pages=10,
        )
        charges = [normalize_charge(r) for r in rows if isinstance(r, dict)]
        charges.sort(key=lambda x: str(x.get("transaction_date") or ""))
        balance = round(sum(float(c.get("balance") or 0) for c in charges), 2)
        oldest = charges[0]["transaction_date"] if charges else None
        payload = {
            "tenant": tenant,
            "balance": balance,
            "open_charge_count": len(charges),
            "oldest_open_charge_date": oldest,
        }
        if include_charges:
            payload["open_charges"] = charges[: _clamp(limit_charges, 1, 200)]
        return _ok(payload, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_payments(tenant_id: int, limit: int = 25) -> str:
    """List recent payments for a tenant (AccountID=TenantID), newest first."""
    try:
        tid = int(tenant_id)
        rows = client().get_all(
            "/Payments",
            filters=f"AccountID,eq,{tid}",
            pagesize=100,
            max_pages=5,
        )
        data = [normalize_payment(r) for r in rows if isinstance(r, dict)]
        data.sort(key=lambda x: str(x.get("transaction_date") or ""), reverse=True)
        data = data[: _clamp(limit, 1, 200)]
        return _ok(data, count=len(data), tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_udfs(parent_type: str | None = None, query: str | None = None, limit: int = 200) -> str:
    """List UserDefinedField definitions. Optional parent_type (Unit/Tenant/...) and name query.

    Note: RM ParentType filter can 400 — full pull + Python filter.
    """
    try:
        rows = _load_udf_catalog()
        pt = (parent_type or "").strip().lower()
        q = (query or "").strip().lower()
        out = []
        for r in rows:
            if pt and str(r.get("parent_type") or "").lower() != pt:
                continue
            if q and q not in str(r.get("name") or "").lower() and q not in str(r.get("field_id") or ""):
                continue
            out.append(r)
            if len(out) >= _clamp(limit, 1, 500):
                break
        return _ok(out, count=len(out), parent_type=parent_type, query=query, known_unit_udfs={
            "731": "List For Lease",
            "732": "Asking Rent",
            "710": "List on Tenant Turner?",
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_describe_resource(name: str | None = None) -> str:
    """Describe known RM resources/filters from local catalog (not live OpenAPI).

    Pass name like Units, Tenants, Charges, ServiceManagerIssues — or omit for full catalog.
    """
    try:
        path = CATALOG_DIR / "resources.json"
        data = json.loads(path.read_text())
        if name:
            key = None
            for k in data.get("resources", {}):
                if k.lower() == name.lower() or name.lower() in k.lower():
                    key = k
                    break
            if not key:
                return _ok(None, found=False, name=name, available=sorted(data.get("resources", {}).keys()))
            return _ok({"name": key, **data["resources"][key]}, found=True, strategy=data.get("strategy"))
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_whoami_capabilities() -> str:
    """Probe auth identity and which key read endpoints respond on this account.

    Records simple GET status for catalog paths. Does not attempt writes.
    """
    try:
        c = client()
        auth = c.auth_status()
        probes = [
            "/Properties",
            "/Units",
            "/Tenants",
            "/Leases",
            "/UserDefinedFields",
            "/ServiceManagerIssues",
            "/Vendors",
            "/Charges",
            "/Payments",
            "/Prospects",
            "/Notes",
            "/Contacts",
        ]
        results = {}
        for path in probes:
            try:
                r = c.request("GET", path, params={"pagesize": 1})
                results[path] = {
                    "status": r.status_code,
                    "ok": True,
                    "total": r.headers.get("X-Total-Results"),
                }
            except Exception as exc:  # noqa: BLE001
                results[path] = {
                    "status": getattr(exc, "status", None),
                    "ok": False,
                    "error": str(exc)[:160],
                }
        return _ok(
            {
                "auth": auth,
                "reads": results,
                "writes": load_write_matrix().get("summary") or {
                    "note": "See rm_write_matrix_status / catalog/write_matrix.json",
                    "account_mode": "mostly_read_only",
                },
                "strategy": "coarse RM filters + Python join/filter/sort",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_notes(limit: int = 20) -> str:
    """List RM /Notes feed. Warning: this instance returns a tiny global set (not full tenant history)."""
    try:
        rows = client().get_all("/Notes", pagesize=50, max_pages=2)
        data = [
            {
                "note_id": r.get("NoteID"),
                "name": r.get("Name"),
                "description": r.get("Description"),
            }
            for r in rows
            if isinstance(r, dict)
        ][: _clamp(limit, 1, 100)]
        return _ok(
            data,
            count=len(data),
            warning="Notes endpoint appears to be templates/global only on this account — not a full history API.",
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)



@mcp.tool()
def rm_write_matrix_status() -> str:
    """Show Phase B write capability matrix for this RM account (from last probe)."""
    try:
        data = load_write_matrix()
        return _ok(data, source=str(CATALOG_DIR / "write_matrix.json"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_list_history(entity_type: str, parent_id: int, limit: int = 25) -> str:
    """List RM history for Tenant|Unit|Property|Vendor|ServiceManagerIssue (read-only)."""
    try:
        et = (entity_type or "").strip()
        pid = int(parent_id)
        path_map = {
            "Tenant": f"/Tenants/{pid}/History",
            "Unit": f"/Units/{pid}/History",
            "Property": f"/Properties/{pid}/History",
            "Vendor": f"/Vendors/{pid}/History",
            "ServiceManagerIssue": f"/ServiceManagerIssues/{pid}/History",
        }
        if et not in path_map:
            return _err(ValueError("entity_type must be Tenant, Unit, Property, Vendor, or ServiceManagerIssue"))
        rows = client().get_all(path_map[et], pagesize=100, max_pages=5)
        data = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            data.append({
                "history_id": r.get("HistoryID"),
                "history_type": r.get("HistoryType"),
                "entity_type": r.get("EntityType") or et,
                "parent_id": r.get("ParentID") or pid,
                "history_date": (str(r.get("HistoryDate") or "")[:10] or None),
                "note": r.get("Note") or r.get("NoteText"),
                "result": r.get("Result") or None,
                "entity_name": r.get("EntityName"),
            })
            if len(data) >= _clamp(limit, 1, 200):
                break
        return _ok(data, count=len(data), entity_type=et, parent_id=pid)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_add_note(
    entity_type: str,
    parent_id: int,
    note: str,
    name: str = "",
    history_category_id: int | None = None,
    history_date: str = "",
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Add a note.

    entity_type:
      - ServiceManagerIssue -> WO history note (preferred ops path)
      - Tenant|Unit|Property|Vendor -> entity HistoryNotes (true history feed)
      - Note -> bare Notes/Items record (global notes list; not entity history)

    Vendor files have no direct documents resource — create a Vendor HistoryNote
    here, then attach with rm_upload_history_attachment (or use
    rm_attach_vendor_file for one-shot).

    history_category_id / history_date are optional (required only if system
    preference RequireCategoryForHistoryNotes is on).

    DEFAULT dry_run=true. Live requires approved_by.
    Note entity_type requires a Name (auto from note text if omitted).
    """
    try:
        et = (entity_type or "").strip()
        pid = int(parent_id)
        text = (note or "").strip()
        if not text:
            return _err(ValueError("note is required"))
        if et not in NOTE_ENTITY_TYPES:
            return _err(ValueError(
                "entity_type must be Tenant, Unit, Property, Vendor, ServiceManagerIssue, or Note"
            ))
        key = (idempotency_key or "").strip() or f"note-{et}-{pid}-{abs(hash(text)) % 10000000}"
        plan = build_add_note_plan(
            entity_type=et,
            parent_id=pid,
            note=text,
            name=(name or None),
            history_category_id=history_category_id,
            history_date=(history_date or None),
        )
        result = guarded_write(
            client(),
            action="add_note",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "agent note",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "entity_type": et,
                "parent_id": pid,
                "name": name or None,
                "history_category_id": history_category_id,
                "history_date": history_date or None,
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_update_unit_udf(
    unit_id: int,
    field_id: int,
    value: str,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Update unit UDF. Allowlisted field_ids: 710, 731, 732.

    DEFAULT dry_run=true. Live requires approved_by.
    Yes/No UDFs (710/731) normalize to Yes/No.
    """
    try:
        uid = int(unit_id)
        fid = int(field_id)
        if fid not in ALLOWLISTED_UDF_FIELD_IDS:
            return _err(ValueError(
                f"field_id {fid} not allowlisted; allowed={sorted(ALLOWLISTED_UDF_FIELD_IDS)}"
            ))
        plan = build_update_unit_udf_plan(unit_id=uid, field_id=fid, value=str(value))
        norm_val = plan[0]["json_body"]["Value"]
        key = (idempotency_key or "").strip() or f"udf-{uid}-{fid}-{norm_val}"
        result = guarded_write(
            client(),
            action="update_unit_udf",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "unit udf update",
            approved_by=approved_by,
            request_plan=plan,
            meta={"unit_id": uid, "field_id": fid, "value": norm_val},
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_create_work_order(
    title: str,
    description: str = "",
    property_id: int | None = None,
    unit_id: int | None = None,
    tenant_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Create a Service Manager work order.

    DEFAULT dry_run=true. Live requires approved_by.
    """
    try:
        ttl = (title or "").strip()
        if not ttl:
            return _err(ValueError("title is required"))
        plan = build_create_work_order_plan(
            title=ttl,
            description=description or "",
            property_id=property_id,
            unit_id=unit_id,
            tenant_id=tenant_id,
        )
        key = (idempotency_key or "").strip() or (
            f"wo-{abs(hash(ttl + str(unit_id) + str(property_id))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="create_work_order",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "create work order",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "property_id": property_id,
                "unit_id": unit_id,
                "tenant_id": tenant_id,
                "title": ttl,
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_update_work_order(
    work_order_id: int,
    title: str | None = None,
    description: str | None = None,
    resolution: str | None = None,
    customer_description: str | None = None,
    note_text: str | None = None,
    status_id: int | None = None,
    assigned_to_user_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Update an existing Service Manager work order.

    RM rejects PATCH/PUT on /ServiceManagerIssues/{id} (405). Updates use
    collection POST upsert with ServiceManagerIssueID, merged from a fresh GET
    snapshot so omitted fields are preserved.

    Mutable fields (pass only what you want to change):
      title, description, resolution, customer_description, note_text,
      status_id, assigned_to_user_id

    Close/IsClosed is a HARD NO-GO for agents (Steve 2026-09-21: WOs are billable; premature close costs real money). Do not implement.
    Linking unit/property/tenant via upsert is also unproven on this account.

    DEFAULT dry_run=true. Live requires approved_by.
    """
    try:
        wid = int(work_order_id)
        provided = {
            "title": title,
            "description": description,
            "resolution": resolution,
            "customer_description": customer_description,
            "note_text": note_text,
            "status_id": status_id,
            "assigned_to_user_id": assigned_to_user_id,
        }
        if all(v is None for v in provided.values()):
            return _err(ValueError("pass at least one field to update"))

        raw, _ = client().get(
            f"/ServiceManagerIssues/{wid}",
            embeds="Status,AssignedToUser",
        )
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict) or raw.get("ServiceManagerIssueID") is None:
            return _err(ValueError(f"work order {wid} not found"))

        plan = build_update_work_order_plan(
            work_order_id=wid,
            current=raw,
            title=title,
            description=description,
            resolution=resolution,
            customer_description=customer_description,
            note_text=note_text,
            status_id=status_id,
            assigned_to_user_id=assigned_to_user_id,
        )
        key = (idempotency_key or "").strip() or (
            f"wo-upd-{wid}-{abs(hash(str(plan[0]['json_body']))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="update_work_order",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "update work order",
            approved_by=approved_by,
            request_plan=plan,
            meta={"work_order_id": wid, "fields": {k: v for k, v in provided.items() if v is not None}},
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_link_work_order(
    work_order_id: int,
    unit_ids: list[int] | None = None,
    property_ids: list[int] | None = None,
    tenant_ids: list[int] | None = None,
    unit_id: int | None = None,
    property_id: int | None = None,
    tenant_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Link unit/property/tenant onto an existing work order.

    Not done via upsert. Uses:
      POST /ServiceManagerIssues/{id}/LinkUnits|LinkProperties|LinkTenants
    Body is a raw JSON array of integer IDs.

    Semantics: APPEND (idempotent if already linked). Does not replace/unlink.
    Unlink routes are not available on this account (404).

    Accepts either list args (unit_ids/property_ids/tenant_ids) or singular
    unit_id/property_id/tenant_id convenience fields.

    DEFAULT dry_run=true. Live requires approved_by.
    """
    try:
        wid = int(work_order_id)

        def _merge(singular: int | None, plural: list[int] | None) -> list[int] | None:
            out: list[int] = []
            if plural:
                out.extend(int(x) for x in plural)
            if singular is not None:
                out.append(int(singular))
            # dedupe preserve order
            seen: set[int] = set()
            deduped: list[int] = []
            for i in out:
                if i in seen:
                    continue
                seen.add(i)
                deduped.append(i)
            return deduped or None

        u = _merge(unit_id, unit_ids)
        p = _merge(property_id, property_ids)
        t = _merge(tenant_id, tenant_ids)
        if not (u or p or t):
            return _err(ValueError("pass at least one unit/property/tenant id"))

        # Ensure WO exists
        raw, _ = client().get(f"/ServiceManagerIssues/{wid}")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict) or raw.get("ServiceManagerIssueID") is None:
            return _err(ValueError(f"work order {wid} not found"))

        plan = build_link_work_order_plan(
            work_order_id=wid,
            unit_ids=u,
            property_ids=p,
            tenant_ids=t,
        )
        key = (idempotency_key or "").strip() or (
            f"wo-link-{wid}-{abs(hash(str(plan))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="link_work_order",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "link work order entities",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "work_order_id": wid,
                "unit_ids": u,
                "property_ids": p,
                "tenant_ids": t,
                "semantics": "append",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            # On live success, return current embeds for confirmation
            if not result.get("dry_run"):
                cur, _ = client().get(
                    f"/ServiceManagerIssues/{wid}",
                    embeds="Units,Properties,Tenants",
                )
                if isinstance(cur, list):
                    cur = cur[0] if cur else {}
                result["linked"] = {
                    "unit_ids": [x.get("UnitID") for x in (cur.get("Units") or [])],
                    "property_ids": [x.get("PropertyID") for x in (cur.get("Properties") or [])],
                    "tenant_ids": [x.get("TenantID") for x in (cur.get("Tenants") or [])],
                }
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_create_task(
    subject: str,
    description: str = "",
    tenant_id: int | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Create an RM Task.

    DEFAULT dry_run=true. Live requires approved_by.
    RM requires Subject (not Title).
    """
    try:
        subj = (subject or "").strip()
        if not subj:
            return _err(ValueError("subject is required"))
        plan = build_create_task_plan(
            subject=subj,
            description=description or "",
            tenant_id=tenant_id,
            property_id=property_id,
            unit_id=unit_id,
        )
        key = (idempotency_key or "").strip() or f"task-{abs(hash(subj)) % 10000000}"
        result = guarded_write(
            client(),
            action="create_task",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "create task",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "subject": subj,
                "tenant_id": tenant_id,
                "property_id": property_id,
                "unit_id": unit_id,
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_upload_unit_image(
    unit_id: int,
    file_path: str,
    image_type_id: int = 1,
    meta_tag: str = "Image1",
    location_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Upload an image to a Unit gallery (NOT a work-order attachment).

    Uses RM support pattern:
      POST /Units/{UnitID}/UploadImage  multipart/form-data
      Payload JSON ties MetaTag -> file field name; ImageTypeID default 1.

    This is unit marketing/gallery imagery. For maintenance WO photos use
    rm_attach_work_order_photo or rm_upload_history_attachment.

    DEFAULT dry_run=true. Live requires approved_by.
    file_path must be a readable local path on the MCP host.
    """
    try:
        uid = int(unit_id)
        path = (file_path or "").strip()
        if not path:
            return _err(ValueError("file_path is required"))
        # Confirm unit exists (read)
        raw, _ = client().get(f"/Units/{uid}")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict) or raw.get("UnitID") is None:
            return _err(ValueError(f"unit {uid} not found"))

        plan = build_upload_unit_image_plan(
            unit_id=uid,
            file_path=path,
            image_type_id=int(image_type_id),
            meta_tag=meta_tag or "Image1",
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            f"unit-img-{uid}-{abs(hash(path + str(image_type_id))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="upload_unit_image",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "upload unit image",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "unit_id": uid,
                "file_path": path,
                "image_type_id": int(image_type_id),
                "meta_tag": meta_tag or "Image1",
                "location_id": location_id,
                "kind": "unit_gallery",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_upload_history_attachment(
    history_id: int,
    file_path: str,
    location_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Attach a file to an existing History note.

    Uses RM support pattern:
      POST /HistoryNotes/{HistoryID}/UploadAttachment  multipart/form-data
      text field File={"HistoryID": N} + binary file part.

    Preferred low-level path once you already have a HistoryID (e.g. from
    rm_add_note on a work order). For one-shot WO photos prefer
    rm_attach_work_order_photo.

    DEFAULT dry_run=true. Live requires approved_by.
    """
    try:
        hid = int(history_id)
        path = (file_path or "").strip()
        if hid <= 0:
            return _err(ValueError("history_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))

        plan = build_upload_history_attachment_plan(
            history_id=hid,
            file_path=path,
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            f"hist-file-{hid}-{abs(hash(path)) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="upload_history_attachment",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "upload history attachment",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "history_id": hid,
                "file_path": path,
                "location_id": location_id,
                "kind": "history_attachment",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_attach_vendor_file(
    vendor_id: int,
    file_path: str,
    note: str = "",
    history_category_id: int | None = None,
    history_date: str = "",
    mode: str = "two_step",
    meta_tag: str = "file1",
    location_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Attach a PDF/file to a vendor via HistoryNotes (no direct vendor docs API).

    RM support 2026-09-27 confirmed:
      - Vendors have no direct file/documents resource
      - Files ride Vendor HistoryNotes (same family as Tenant/Unit/Property)
      - Option A (mode=one_request): multipart create note + file
      - Option B (mode=two_step, default): create note, then UploadAttachment
      - POST /Vendors/{id}/History is 405 — use /HistoryNotes
      - Privilege min: History > Vendor History/Notes > Add

    DEFAULT dry_run=true. Live requires approved_by.
    file_path must be readable on the MCP host.
    """
    try:
        vid = int(vendor_id)
        path = (file_path or "").strip()
        if vid <= 0:
            return _err(ValueError("vendor_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))

        plan = build_attach_vendor_file_plan(
            vendor_id=vid,
            file_path=path,
            note=note or "",
            history_category_id=history_category_id,
            history_date=(history_date or None),
            location_id=location_id,
            mode=mode or "two_step",
            meta_tag=meta_tag or "file1",
        )
        key = (idempotency_key or "").strip() or (
            f"vendor-file-{vid}-{abs(hash(path + (note or ''))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="attach_vendor_file",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "attach vendor file",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "vendor_id": vid,
                "file_path": path,
                "mode": mode or "two_step",
                "history_category_id": history_category_id,
                "history_date": history_date or None,
                "location_id": location_id,
                "kind": "vendor_history_attachment",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def rm_attach_work_order_photo(
    work_order_id: int,
    file_path: str,
    note: str = "",
    location_id: int | None = None,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Attach a photo/file to a work order via history note + upload.

    Two-step plan (progress path from RM support collection):
      1) POST /ServiceManagerIssues/{id}/History  (create note; returns HistoryID)
      2) POST /HistoryNotes/{HistoryID}/UploadAttachment  (multipart file)

    This is the intended maintenance WO photo path. Unit UploadImage is a
    different gallery feature — do not use that for crew WO photos.

    DEFAULT dry_run=true. Live requires approved_by.
    file_path must be readable on the MCP host.
    """
    try:
        wid = int(work_order_id)
        path = (file_path or "").strip()
        if wid <= 0:
            return _err(ValueError("work_order_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))

        raw, _ = client().get(f"/ServiceManagerIssues/{wid}")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict) or raw.get("ServiceManagerIssueID") is None:
            return _err(ValueError(f"work order {wid} not found"))

        plan = build_attach_work_order_photo_plan(
            work_order_id=wid,
            file_path=path,
            note=note or "",
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            f"wo-photo-{wid}-{abs(hash(path + (note or ''))) % 10000000}"
        )
        result = guarded_write(
            client(),
            action="attach_work_order_photo",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "attach work order photo",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "work_order_id": wid,
                "file_path": path,
                "note": note or None,
                "location_id": location_id,
                "kind": "work_order_photo",
                "steps": ["create_history_note", "upload_attachment"],
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)



@mcp.tool()
def rm_search_bills(
    account_id: int | None = None,
    reference: str | None = None,
    due_date: str | None = None,
    amount: float | None = None,
    reference_contains: str | None = None,
    limit: int = 25,
    location_id: int | None = None,
) -> str:
    """Search vendor bills via /Bills/Search (dup-check helper).

    IMPORTANT: filter on AccountID (vendor id), NOT VendorID.
    HTTP 204 / empty means no match (safe to create).
    """
    try:
        parts: list[str] = []
        if account_id is not None:
            parts.append(f"AccountID,eq,{int(account_id)}")
        if reference is not None and str(reference).strip():
            parts.append(f"Reference,eq,{str(reference).strip()}")
        if reference_contains is not None and str(reference_contains).strip():
            parts.append(f"Reference,ct,{str(reference_contains).strip()}")
        if due_date is not None and str(due_date).strip():
            d = str(due_date).strip().split("T", 1)[0][:10]
            parts.append(f"DueDate,eq,{d}")
        if amount is not None:
            parts.append(f"Amount,eq,{float(amount)}")
        if not parts:
            return _err(ValueError("provide at least one search field"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        params = {
            "filterExpression": ";".join(parts),
            "fields": "ID,AccountID,Reference,DueDate,Amount,Comment,TransactionDate,PostDate,AccountType",
            "pageSize": _clamp(int(limit), 1, 200),
        }
        r = client().request("GET", "/Bills/Search", params=params, extra_headers=headers or None)
        if r.status_code == 204 or not r.content:
            return _ok([], count=0, matched=False, filter=params["filterExpression"], location_id=location_id)
        data = r.json()
        if not isinstance(data, list):
            data = [data] if data else []
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            out.append({
                "bill_id": row.get("ID"),
                "account_id": row.get("AccountID"),
                "reference": row.get("Reference"),
                "due_date": row.get("DueDate"),
                "amount": row.get("Amount"),
                "comment": row.get("Comment"),
                "transaction_date": row.get("TransactionDate"),
                "post_date": row.get("PostDate"),
                "account_type": row.get("AccountType"),
            })
        return _ok(out, count=len(out), matched=bool(out), filter=params["filterExpression"], location_id=location_id)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_get_bill(bill_id: int, include_details: bool = True, location_id: int | None = None) -> str:
    """Get one bill by ID, optionally with BillDetails."""
    try:
        bid = int(bill_id)
        if bid <= 0:
            return _err(ValueError("bill_id must be positive"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        params: dict[str, Any] = {}
        if include_details:
            params["embeds"] = "BillDetails"
        r = client().request("GET", f"/Bills/{bid}", params=params or None, extra_headers=headers or None)
        if r.status_code == 204 or not r.content:
            return _ok(None, found=False, bill_id=bid)
        raw = r.json()
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return _ok(None, found=False, bill_id=bid)
        details = []
        for d in raw.get("BillDetails") or []:
            if not isinstance(d, dict):
                continue
            details.append({
                "bill_detail_id": d.get("BillDetailID"),
                "property_id": d.get("PropertyID"),
                "unit_id": d.get("UnitID"),
                "gl_account_id": d.get("GLAccountID"),
                "amount": d.get("Amount"),
                "comment": d.get("Comment"),
            })
        dto = {
            "bill_id": raw.get("ID"),
            "account_id": raw.get("AccountID"),
            "account_type": raw.get("AccountType"),
            "reference": raw.get("Reference"),
            "comment": raw.get("Comment"),
            "amount": raw.get("Amount"),
            "transaction_date": raw.get("TransactionDate"),
            "post_date": raw.get("PostDate"),
            "due_date": raw.get("DueDate"),
            "bank_id": raw.get("BankID"),
            "is_approved": raw.get("IsApproved"),
            "details": details,
        }
        return _ok(dto, found=True)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_list_gl_accounts(query: str | None = None, active_only: bool = False, limit: int = 50) -> str:
    """List GL accounts. Optional name/reference contains filter.

    Note: IsActive flags are often stale. Historical tax bills used GL 544 (Taxes/6100).
    """
    try:
        rows = client().get_all(
            "/GLAccounts",
            fields="GLAccountID,Name,Reference,IsActive,GLAccountType",
            pagesize=200,
            max_pages=10,
        )
        q = (query or "").strip().lower()
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = str(r.get("Name") or "")
            ref = str(r.get("Reference") or "")
            if q and q not in name.lower() and q not in ref.lower():
                continue
            if active_only and not bool(r.get("IsActive")):
                continue
            out.append({
                "gl_account_id": r.get("GLAccountID"),
                "name": name,
                "reference": ref,
                "is_active": r.get("IsActive"),
                "gl_account_type": r.get("GLAccountType"),
            })
            if len(out) >= _clamp(limit, 1, 500):
                break
        return _ok(out, count=len(out), query=query, active_only=active_only)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_create_vendor_bill(
    account_id: int,
    property_id: int,
    gl_account_id: int,
    amount: float,
    transaction_date: str,
    post_date: str,
    due_date: str,
    reference: str = "",
    comment: str = "",
    detail_comment: str = "",
    unit_id: int | None = None,
    bill_details_json: str = "",
    file_path: str = "",
    file_description: str = "",
    location_id: int | None = 1,
    skip_duplicate_check: bool = False,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Create a vendor bill (Payables > Bills). Default dry_run=true; live needs approved_by."""
    try:
        details: list[dict[str, Any]]
        raw_details = (bill_details_json or "").strip()
        if raw_details:
            parsed = json.loads(raw_details)
            if not isinstance(parsed, list) or not parsed:
                return _err(ValueError("bill_details_json must be a non-empty JSON array"))
            details = parsed
        else:
            line: dict[str, Any] = {"PropertyID": int(property_id), "GLAccountID": int(gl_account_id), "Amount": float(amount)}
            if detail_comment:
                line["Comment"] = detail_comment
            elif comment:
                line["Comment"] = comment
            if unit_id is not None and int(unit_id) > 0:
                line["UnitID"] = int(unit_id)
            details = [line]
        if not skip_duplicate_check and (reference or "").strip():
            filt = [f"AccountID,eq,{int(account_id)}", f"Reference,eq,{str(reference).strip()}"]
            if due_date:
                filt.append("DueDate,eq," + str(due_date).strip().split("T", 1)[0][:10])
            headers: dict[str, str] = {}
            if location_id is not None:
                headers["x-rm12api-locationid"] = str(int(location_id))
            sr = client().request("GET", "/Bills/Search", params={"filterExpression": ";".join(filt), "fields": "ID,AccountID,Reference,DueDate,Amount,Comment", "pageSize": 10}, extra_headers=headers or None)
            if sr.status_code != 204 and sr.content:
                existing = sr.json()
                if isinstance(existing, list) and existing:
                    return json.dumps({"ok": False, "error": {"code": "duplicate_bill", "message": "Matching bill(s) already exist"}, "data": {"matched": existing, "filter": ";".join(filt), "dry_run": bool(dry_run)}}, default=str)
        plan, warnings = build_create_vendor_bill_plan(account_id=int(account_id), transaction_date=transaction_date, post_date=post_date, due_date=due_date, bill_details=details, reference=reference or "", comment=comment or "", file_path=(file_path or "").strip() or None, file_description=file_description or "", location_id=location_id)
        key = (idempotency_key or "").strip() or ("bill-%s-%s-%s-%s" % (int(account_id), (reference or "").strip() or "noref", str(due_date)[:10], abs(hash(json.dumps(details, sort_keys=True))) % 10000000))
        result = guarded_write(client(), action="create_vendor_bill", dry_run=bool(dry_run), idempotency_key=key, reason=reason or "create vendor bill", approved_by=approved_by, request_plan=plan, meta={"account_id": int(account_id), "reference": reference or None, "due_date": due_date, "location_id": location_id, "file_path": (file_path or None) or None, "validation_warnings": warnings, "duplicate_check": "skipped" if skip_duplicate_check else "AccountID+Reference+DueDate", "kind": "vendor_bill_create"})
        if warnings:
            result = {**result, "validation_warnings": warnings}
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)

@mcp.tool()
def rm_attach_bill_file(bill_id: int, file_path: str, description: str = "", mode: str = "upload", meta_tag: str = "File1", location_id: int | None = 1, dry_run: bool = True, idempotency_key: str = "", reason: str = "", approved_by: str | None = None) -> str:
    """Attach a file/PDF to an existing vendor bill. dry_run default; live needs approved_by."""
    try:
        bid = int(bill_id)
        path = (file_path or "").strip()
        if bid <= 0:
            return _err(ValueError("bill_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        br = client().request("GET", f"/Bills/{bid}", params={"fields": "ID,AccountID,Reference,Amount"}, extra_headers=headers or None)
        if br.status_code == 204 or not br.content:
            return _err(ValueError("bill %s not found" % bid))
        plan = build_attach_bill_file_plan(bill_id=bid, file_path=path, description=description or "", mode=mode or "upload", meta_tag=meta_tag or "File1", location_id=location_id)
        key = (idempotency_key or "").strip() or ("bill-file-%s-%s" % (bid, abs(hash(path + (mode or "") + (meta_tag or ""))) % 10000000))
        result = guarded_write(client(), action="attach_bill_file", dry_run=bool(dry_run), idempotency_key=key, reason=reason or "attach bill file", approved_by=approved_by, request_plan=plan, meta={"bill_id": bid, "file_path": path, "mode": mode, "location_id": location_id, "kind": "vendor_bill_attachment"})
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)

@mcp.tool()
def rm_update_vendor_bill(bill_id: int, fields_json: str, location_id: int | None = 1, dry_run: bool = True, idempotency_key: str = "", reason: str = "", approved_by: str | None = None) -> str:
    """Update vendor bill via POST upsert + fields= mask. dry_run default; live needs approved_by."""
    try:
        bid = int(bill_id)
        if bid <= 0:
            return _err(ValueError("bill_id must be positive"))
        raw = (fields_json or "").strip()
        if not raw:
            return _err(ValueError("fields_json is required"))
        fields = json.loads(raw)
        if not isinstance(fields, dict) or not fields:
            return _err(ValueError("fields_json must be a non-empty object"))
        plan = build_update_vendor_bill_plan(bill_id=bid, fields=fields, location_id=location_id)
        key = (idempotency_key or "").strip() or ("bill-upd-%s-%s" % (bid, abs(hash(raw)) % 10000000))
        result = guarded_write(client(), action="update_vendor_bill", dry_run=bool(dry_run), idempotency_key=key, reason=reason or "update vendor bill", approved_by=approved_by, request_plan=plan, meta={"bill_id": bid, "fields": fields, "location_id": location_id, "kind": "vendor_bill_update"})
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)



@mcp.tool()
def rm_list_credit_cards(active_only: bool = True, query: str | None = None, limit: int = 50) -> str:
    """List credit card accounts. Lookup only. Does not post a charge."""
    try:
        params: dict[str, Any] = {"fields": "CreditCardID,Name,Reference,IsActive"}
        if active_only:
            params["filters"] = "IsActive,eq,true"
        r = client().request("GET", "/CreditCards", params=params)
        if r.status_code == 204 or not r.content:
            return _ok([], count=0)
        data = r.json()
        if not isinstance(data, list):
            data = [data] if data else []
        q = (query or "").strip().lower()
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "")
            ref = str(row.get("Reference") or "")
            if q and q not in name.lower() and q not in ref.lower() and q not in str(row.get("CreditCardID") or ""):
                continue
            out.append({
                "credit_card_id": row.get("CreditCardID"),
                "name": name,
                "reference": ref,
                "is_active": row.get("IsActive"),
            })
            if len(out) >= _clamp(int(limit), 1, 200):
                break
        return _ok(out, count=len(out), active_only=active_only, query=query)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_get_credit_card_transaction(transaction_id: int, include_attachments: bool = False, location_id: int | None = None) -> str:
    """Read one credit card charge by ID. Does not post or pay anything."""
    try:
        tid = int(transaction_id)
        if tid <= 0:
            return _err(ValueError("transaction_id must be positive"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        embeds = "CreditCardTransactionDetails,Attachments" if include_attachments else "CreditCardTransactionDetails"
        r = client().request(
            "GET",
            f"/CreditCardTransactions/{tid}",
            params={"embeds": embeds},
            extra_headers=headers or None,
        )
        if r.status_code == 204 or not r.content:
            return _ok(None, found=False, transaction_id=tid)
        raw = r.json()
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return _ok(None, found=False, transaction_id=tid)
        details = []
        for d in raw.get("CreditCardTransactionDetails") or []:
            if not isinstance(d, dict):
                continue
            details.append({
                "detail_id": d.get("CreditCardTransactionDetailID"),
                "property_id": d.get("PropertyID"),
                "unit_id": d.get("UnitID"),
                "gl_account_id": d.get("GLAccountID"),
                "amount": d.get("Amount"),
                "comment": d.get("Comment"),
            })
        atts = []
        for a in raw.get("Attachments") or []:
            if not isinstance(a, dict):
                continue
            atts.append({
                "file_attachment_id": a.get("FileAttachmentID"),
                "description": a.get("Description"),
            })
        dto = {
            "transaction_id": raw.get("ID"),
            "credit_card_id": raw.get("CreditCardID"),
            "account_id": raw.get("AccountID"),
            "account_type": raw.get("AccountType"),
            "reference": raw.get("Reference"),
            "comment": raw.get("Comment"),
            "amount": raw.get("Amount"),
            "transaction_date": raw.get("TransactionDate"),
            "details": details,
            "attachments": atts,
        }
        return _ok(dto, found=True)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_create_credit_card_transaction(
    credit_card_id: int,
    account_id: int,
    property_id: int,
    gl_account_id: int,
    amount: float,
    transaction_date: str,
    reference: str = "",
    comment: str = "",
    detail_comment: str = "",
    unit_id: int | None = None,
    details_json: str = "",
    file_path: str = "",
    file_description: str = "",
    meta_tag: str = "file1",
    location_id: int | None = 1,
    skip_duplicate_check: bool = False,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Post a credit card charge (not a vendor bill, not a rent receipt).

    Default dry_run=true. Live needs approved_by.
    Payee account_id is a VendorID. Header Amount is not sent.
    A file is attached only when file_path is set, and only if embeds include Attachments.
    """
    try:
        raw_details = (details_json or "").strip()
        if raw_details:
            parsed = json.loads(raw_details)
            if not isinstance(parsed, list) or not parsed:
                return _err(ValueError("details_json must be a non-empty JSON array"))
            details = parsed
        else:
            line: dict[str, Any] = {
                "PropertyID": int(property_id),
                "GLAccountID": int(gl_account_id),
                "Amount": float(amount),
            }
            if detail_comment:
                line["Comment"] = detail_comment
            elif comment:
                line["Comment"] = comment
            if unit_id is not None and int(unit_id) > 0:
                line["UnitID"] = int(unit_id)
            details = [line]
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        dup_note = "skipped" if skip_duplicate_check else "none"
        if not skip_duplicate_check and (reference or "").strip():
            filt = [
                f"CreditCardID,eq,{int(credit_card_id)}",
                f"AccountID,eq,{int(account_id)}",
                f"Reference,eq,{str(reference).strip()}",
            ]
            if transaction_date:
                filt.append("TransactionDate,eq," + str(transaction_date).strip().split("T", 1)[0][:10])
            sr = client().request(
                "GET",
                "/CreditCardTransactions",
                params={
                    "filters": ";".join(filt),
                    "fields": "ID,CreditCardID,AccountID,Reference,TransactionDate,Amount,Comment",
                    "pageSize": 10,
                },
                extra_headers=headers or None,
            )
            if sr.status_code not in (200, 204) and sr.content:
                dup_note = f"lookup_http_{sr.status_code}"
            elif sr.status_code != 204 and sr.content:
                existing = sr.json()
                if isinstance(existing, dict):
                    existing = [existing]
                if isinstance(existing, list) and existing:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "code": "duplicate_credit_card_transaction",
                                "message": "Matching credit card charge(s) already exist",
                            },
                            "data": {"matched": existing, "filter": ";".join(filt), "dry_run": bool(dry_run)},
                        },
                        default=str,
                    )
                dup_note = "CreditCardID+AccountID+Reference+TransactionDate"
            else:
                dup_note = "CreditCardID+AccountID+Reference+TransactionDate"
        plan, warnings = build_create_credit_card_transaction_plan(
            credit_card_id=int(credit_card_id),
            account_id=int(account_id),
            transaction_date=transaction_date,
            details=details,
            reference=reference or "",
            comment=comment or "",
            file_path=(file_path or "").strip() or None,
            file_description=file_description or "",
            meta_tag=meta_tag or "file1",
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            "cc-%s-%s-%s-%s"
            % (
                int(credit_card_id),
                int(account_id),
                (reference or "").strip() or "noref",
                str(transaction_date)[:10],
            )
        )
        result = guarded_write(
            client(),
            action="create_credit_card_transaction",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "create credit card charge",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "credit_card_id": int(credit_card_id),
                "account_id": int(account_id),
                "reference": reference or None,
                "transaction_date": transaction_date,
                "location_id": location_id,
                "file_path": (file_path or None) or None,
                "validation_warnings": warnings,
                "duplicate_check": dup_note,
                "kind": "credit_card_transaction_create",
            },
        )
        if warnings:
            result = {**result, "validation_warnings": warnings}
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_attach_credit_card_file(
    transaction_id: int,
    file_path: str,
    description: str = "",
    meta_tag: str = "file1",
    location_id: int | None = 1,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Attach a scan to an existing credit card charge. dry_run default; live needs approved_by."""
    try:
        tid = int(transaction_id)
        path = (file_path or "").strip()
        if tid <= 0:
            return _err(ValueError("transaction_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        br = client().request(
            "GET",
            f"/CreditCardTransactions/{tid}",
            params={"fields": "ID,CreditCardID,AccountID,Reference,Amount"},
            extra_headers=headers or None,
        )
        if br.status_code == 204 or not br.content:
            return _err(ValueError("credit card transaction %s not found" % tid))
        plan = build_attach_credit_card_file_plan(
            transaction_id=tid,
            file_path=path,
            description=description or "",
            meta_tag=meta_tag or "file1",
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            "cc-file-%s-%s" % (tid, abs(hash(path + (meta_tag or ""))) % 10000000)
        )
        result = guarded_write(
            client(),
            action="attach_credit_card_file",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "attach credit card scan",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "transaction_id": tid,
                "file_path": path,
                "location_id": location_id,
                "kind": "credit_card_attachment",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_update_credit_card_transaction(
    transaction_id: int,
    fields_json: str,
    location_id: int | None = 1,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Update memo/reference/date on a credit card charge via POST + fields= mask.

    Does not change amount, detail lines, or attachments. dry_run default; live needs approved_by.
    """
    try:
        tid = int(transaction_id)
        if tid <= 0:
            return _err(ValueError("transaction_id must be positive"))
        raw = (fields_json or "").strip()
        if not raw:
            return _err(ValueError("fields_json is required"))
        fields = json.loads(raw)
        if not isinstance(fields, dict) or not fields:
            return _err(ValueError("fields_json must be a non-empty object"))
        plan = build_update_credit_card_transaction_plan(
            transaction_id=tid, fields=fields, location_id=location_id
        )
        key = (idempotency_key or "").strip() or ("cc-upd-%s-%s" % (tid, abs(hash(raw)) % 10000000))
        result = guarded_write(
            client(),
            action="update_credit_card_transaction",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "update credit card charge",
            approved_by=approved_by,
            request_plan=plan,
            meta={"transaction_id": tid, "fields": fields, "location_id": location_id, "kind": "credit_card_update"},
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_list_banks(query: str | None = None, limit: int = 50) -> str:
    """List bank accounts Lucy can use as BankID. Lookup only. Does not write a check."""
    try:
        r = client().request("GET", "/Banks", params={"fields": "BankID,Name"})
        if r.status_code == 204 or not r.content:
            return _ok([], count=0)
        data = r.json()
        if not isinstance(data, list):
            data = [data] if data else []
        q = (query or "").strip().lower()
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "")
            if q and q not in name.lower() and q not in str(row.get("BankID") or ""):
                continue
            out.append({"bank_id": row.get("BankID"), "name": name})
            if len(out) >= _clamp(int(limit), 1, 200):
                break
        return _ok(out, count=len(out), query=query)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_get_check(check_id: int, include_attachments: bool = False, location_id: int | None = None) -> str:
    """Read one check by ID. Does not post a payment or a rent receipt."""
    try:
        cid = int(check_id)
        if cid <= 0:
            return _err(ValueError("check_id must be positive"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        embeds = "CheckDetails,FileAttachments" if include_attachments else "CheckDetails"
        r = client().request(
            "GET",
            f"/Checks/{cid}",
            params={"embeds": embeds},
            extra_headers=headers or None,
        )
        if r.status_code == 204 or not r.content:
            return _ok(None, found=False, check_id=cid)
        raw = r.json()
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return _ok(None, found=False, check_id=cid)
        details = []
        for d in raw.get("CheckDetails") or []:
            if not isinstance(d, dict):
                continue
            details.append({
                "check_detail_id": d.get("CheckDetailID"),
                "property_id": d.get("PropertyID"),
                "unit_id": d.get("UnitID"),
                "gl_account_id": d.get("GLAccountID"),
                "amount": d.get("Amount"),
                "comment": d.get("Comment"),
                "update_date": d.get("UpdateDate"),
            })
        atts = []
        for a in raw.get("FileAttachments") or []:
            if not isinstance(a, dict):
                continue
            atts.append({
                "file_attachment_id": a.get("FileAttachmentID"),
                "file_id": a.get("FileID"),
                "description": a.get("Description"),
            })
        dto = {
            "check_id": raw.get("ID"),
            "bank_id": raw.get("BankID"),
            "account_id": raw.get("AccountID"),
            "account_type": raw.get("AccountType"),
            "reference": raw.get("Reference"),
            "comment": raw.get("Comment"),
            "payee": raw.get("Payee"),
            "amount": raw.get("Amount"),
            "transaction_date": raw.get("TransactionDate"),
            "details": details,
            "attachments": atts,
        }
        return _ok(dto, found=True)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_create_check(
    bank_id: int,
    account_id: int,
    property_id: int,
    gl_account_id: int,
    amount: float,
    transaction_date: str,
    payee: str = "",
    reference: str = "",
    comment: str = "",
    detail_comment: str = "",
    unit_id: int | None = None,
    details_json: str = "",
    file_path: str = "",
    file_description: str = "",
    meta_tag: str = "File1",
    ignore_prop_default_bank_warning: bool = False,
    location_id: int | None = 1,
    skip_duplicate_check: bool = False,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Record a paid receipt as a check written from a bank. Not a rent receipt. Not a vendor bill.

    Default dry_run=true. Live needs approved_by.
    account_id is the payee VendorID. Header Amount is not sent.
    A file is attached only when file_path is set, and only if embeds include FileAttachments.
    """
    try:
        raw_details = (details_json or "").strip()
        if raw_details:
            parsed = json.loads(raw_details)
            if not isinstance(parsed, list) or not parsed:
                return _err(ValueError("details_json must be a non-empty JSON array"))
            details = parsed
        else:
            line: dict[str, Any] = {
                "PropertyID": int(property_id),
                "GLAccountID": int(gl_account_id),
                "Amount": float(amount),
            }
            if detail_comment:
                line["Comment"] = detail_comment
            elif comment:
                line["Comment"] = comment
            if unit_id is not None and int(unit_id) > 0:
                line["UnitID"] = int(unit_id)
            details = [line]
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        dup_note = "skipped" if skip_duplicate_check else "none"
        if not skip_duplicate_check and (reference or "").strip():
            filt = [
                f"BankID,eq,{int(bank_id)}",
                f"AccountID,eq,{int(account_id)}",
                f"Reference,eq,{str(reference).strip()[:25]}",
            ]
            if transaction_date:
                filt.append("TransactionDate,eq," + str(transaction_date).strip().split("T", 1)[0][:10])
            try:
                sr = client().request(
                    "GET",
                    "/Checks",
                    params={
                        "filters": ";".join(filt),
                        "fields": "ID,BankID,AccountID,Reference,TransactionDate,Amount,Comment,Payee",
                        "pageSize": 10,
                    },
                    extra_headers=headers or None,
                )
                if sr.status_code == 404:
                    dup_note = "checks_list_404_no_dup_check"
                elif sr.status_code not in (200, 204) and sr.content:
                    dup_note = f"lookup_http_{sr.status_code}"
                elif sr.status_code != 204 and sr.content:
                    existing = sr.json()
                    if isinstance(existing, dict):
                        existing = [existing]
                    if isinstance(existing, list) and existing:
                        return json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "duplicate_check",
                                    "message": "Matching check(s) already exist",
                                },
                                "data": {"matched": existing, "filter": ";".join(filt), "dry_run": bool(dry_run)},
                            },
                            default=str,
                        )
                    dup_note = "BankID+AccountID+Reference+TransactionDate"
                else:
                    dup_note = "BankID+AccountID+Reference+TransactionDate"
            except Exception as dup_exc:
                status = getattr(dup_exc, "status", None)
                dup_note = "checks_list_404_no_dup_check" if status == 404 else f"lookup_failed_{status or type(dup_exc).__name__}"
        plan, warnings = build_create_check_plan(
            bank_id=int(bank_id),
            account_id=int(account_id),
            transaction_date=transaction_date,
            details=details,
            reference=reference or "",
            comment=comment or "",
            payee=payee or "",
            file_path=(file_path or "").strip() or None,
            file_description=file_description or "",
            meta_tag=meta_tag or "File1",
            ignore_prop_default_bank_warning=bool(ignore_prop_default_bank_warning),
            location_id=location_id,
        )
        if dup_note.startswith("lookup_failed") or dup_note.endswith("no_dup_check"):
            warnings.append(dup_note)
        key = (idempotency_key or "").strip() or (
            "chk-%s-%s-%s-%s"
            % (
                int(bank_id),
                int(account_id),
                (reference or "").strip()[:25] or "noref",
                str(transaction_date)[:10],
            )
        )
        result = guarded_write(
            client(),
            action="create_check",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "create paid receipt check",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "bank_id": int(bank_id),
                "account_id": int(account_id),
                "reference": reference or None,
                "payee": payee or None,
                "transaction_date": transaction_date,
                "location_id": location_id,
                "file_path": (file_path or None) or None,
                "ignore_prop_default_bank_warning": bool(ignore_prop_default_bank_warning),
                "validation_warnings": warnings,
                "duplicate_check": dup_note,
                "kind": "check_create",
            },
        )
        if warnings:
            result = {**result, "validation_warnings": warnings}
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_attach_check_file(
    check_id: int,
    file_path: str,
    description: str = "",
    meta_tag: str = "File1",
    location_id: int | None = 1,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Attach a scan to an existing check. dry_run default; live needs approved_by. Not a rent receipt."""
    try:
        cid = int(check_id)
        path = (file_path or "").strip()
        if cid <= 0:
            return _err(ValueError("check_id must be positive"))
        if not path:
            return _err(ValueError("file_path is required"))
        headers: dict[str, str] = {}
        if location_id is not None:
            headers["x-rm12api-locationid"] = str(int(location_id))
        br = client().request(
            "GET",
            f"/Checks/{cid}",
            params={"fields": "ID,BankID,AccountID,Reference,Amount"},
            extra_headers=headers or None,
        )
        if br.status_code == 204 or not br.content:
            return _err(ValueError("check %s not found" % cid))
        plan = build_attach_check_file_plan(
            check_id=cid,
            file_path=path,
            description=description or "",
            meta_tag=meta_tag or "File1",
            location_id=location_id,
        )
        key = (idempotency_key or "").strip() or (
            "chk-file-%s-%s" % (cid, abs(hash(path + (meta_tag or ""))) % 10000000)
        )
        result = guarded_write(
            client(),
            action="attach_check_file",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "attach check scan",
            approved_by=approved_by,
            request_plan=plan,
            meta={
                "check_id": cid,
                "file_path": path,
                "location_id": location_id,
                "kind": "check_attachment",
            },
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rm_update_check(
    check_id: int,
    fields_json: str,
    location_id: int | None = 1,
    dry_run: bool = True,
    idempotency_key: str = "",
    reason: str = "",
    approved_by: str | None = None,
) -> str:
    """Update memo, reference, payee, or date on a check via POST + fields= mask.

    Does not change amount, detail lines, bank, or vendor. dry_run default; live needs approved_by.
    """
    try:
        cid = int(check_id)
        if cid <= 0:
            return _err(ValueError("check_id must be positive"))
        raw = (fields_json or "").strip()
        if not raw:
            return _err(ValueError("fields_json is required"))
        fields = json.loads(raw)
        if not isinstance(fields, dict) or not fields:
            return _err(ValueError("fields_json must be a non-empty object"))
        plan = build_update_check_plan(check_id=cid, fields=fields, location_id=location_id)
        key = (idempotency_key or "").strip() or ("chk-upd-%s-%s" % (cid, abs(hash(raw)) % 10000000))
        result = guarded_write(
            client(),
            action="update_check",
            dry_run=bool(dry_run),
            idempotency_key=key,
            reason=reason or "update check",
            approved_by=approved_by,
            request_plan=plan,
            meta={"check_id": cid, "fields": fields, "location_id": location_id, "kind": "check_update"},
        )
        if result.get("ok") or result.get("dry_run"):
            return _ok(result)
        return json.dumps({"ok": False, "error": result.get("error"), "data": result}, default=str)
    except Exception as exc:
        return _err(exc)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
