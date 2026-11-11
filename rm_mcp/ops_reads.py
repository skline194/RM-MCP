"""Read-only prospect, renewal, agenda, and report helpers.

Every Rent Manager call from this module is a GET. The expiring-lease read
sends ActiveLeaseRenewal.EndDate,ge and ,le for the window, plus PropertyID
when a property is passed. Other reads send at most one simple id filter.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from .client import PageList, RMError
from .normalize import (
    normalize_appointment,
    normalize_increase_offer,
    normalize_interested_rental,
    normalize_lead_source,
    normalize_lease_renewal,
    normalize_lost_reason,
    normalize_lost_reason_lookup,
    normalize_memorized_report,
    normalize_prospect_address,
    normalize_recurring_charge,
    normalize_prospect_card,
    normalize_prospect_contact,
    normalize_prospect_stage,
    normalize_report_listing,
    normalize_report_parameter,
    normalize_saved_report_value,
    normalize_stage_history,
    normalize_task,
    normalize_user_brief,
)

PROSPECT_LIST_EMBEDS = "LeadSource,CurrentProspectStage,Contacts,PrimaryContact"
PROSPECT_DETAIL_EMBEDS = (
    "LeadSource,CurrentProspectStage,ProspectLostReason,Contacts,PrimaryContact,"
    "Addresses,Contacts.PhoneNumbers,PrimaryContact.PhoneNumbers"
)
LEASE_LIST_EMBEDS = "LeaseRenewals,ActiveLeaseRenewal,Tenant,Unit"
LEASE_DETAIL_EMBEDS = "LeaseRenewals,ActiveLeaseRenewal,Tenant,Unit,Property"
APPOINTMENT_EMBEDS = "AppointmentUsers,Tenant,Prospect"
TASK_EMBEDS = "TaskUsers,Tenant,Prospect"
REPORT_LIST_EMBEDS = "ReportCategory"
MEMORIZED_LIST_EMBEDS = "Report,Report.ReportCategory"

_REPORT_ROW_CAP = 80
_REPORT_TEXT_CAP = 24000
_APPOINTMENT_WINDOW_DAYS = 62
_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# These names are the usual Rent Manager Url getOptions. The crawled help page
# lists getOptions but not the enum members, so they are sent only when asked.
_REPORT_GET_OPTIONS = {
    "csv": "ReturnCSVUrl",
    "excel": "ReturnExcelUrl",
    "html": "ReturnHTMLUrl",
    "text": "ReturnTextUrl",
    "pdf": "ReturnPDFUrl",
}

RENEWAL_NOTE = (
    "Lease end is the EndDate of the renewal term in effect today "
    "(StartDate on or before today, and EndDate on or after today). "
    "The calculated Lease.EndDate is used only when that term has no EndDate. "
    "ExpectedMoveOutDate is reported separately and is not the lease end. "
    "renewal_status is about a later LeaseRenewal, not the current term: "
    "no_renewal, signed (the later term has SignDate), or renewal_on_file. "
    "A current term is itself a LeaseRenewal, so renewal_count can be 1 while "
    "renewal_status is no_renewal. LeaseRenewal has no Status field. "
    "signable_document_status is the raw SignableDocumentStatus from that later "
    "term. The help sample does not define its values. "
    "A lease has ended when MoveOutDate or DepartureDate is today or earlier, "
    "compared as a calendar date (the first 10 characters, not a timezone shift). "
    "Blank dates and placeholder dates on or before 1900-01-01 (including 0001-01-01) "
    "are not a move-out. A future MoveOutDate is not a completed move-out. "
    "The term window is ActiveLeaseRenewal.EndDate from today through the horizon. "
    "A second read keeps still-occupied leases whose MoveOutDate or ExpectedMoveOutDate "
    "falls in that same horizon, including month-to-month leases and leases whose term "
    "ends later. Those rows are notice. The same LeaseID is kept once. "
    "If a move-out filter is rejected, unit CurrentOccupancyStatus is used instead. "
    "If the term filter is rejected, the list is read without date filters and the "
    "window is applied here. A cutoff on that download is flagged. "
    "open_ended is a number only when a complete unfiltered download was read. "
    "Otherwise it is null and open_ended_note says it was not counted."
)
RENT_NOTE = (
    "current_rent is base rent: charge type RC plus subsidized RC8, the same "
    "codes as the rent roll. Surcharges are RAC and RWD. Concessions are RCI. "
    "Every other named code is other. Names match exactly, ignoring case. "
    "Comments are not used. GL account numbers do not classify a charge. "
    "Only charges in effect today count. The same RecurringChargeID on the tenant "
    "and the unit is counted once, tenant first. "
    "With property_id, tenant and unit recurring charges are one collection read each "
    "(embeds RecurringCharges; tenants also embed Leases). RecurringCharges.ChargeType "
    "is not a tenant or unit embed. Charge type names are joined from one GET /ChargeTypes "
    "by ChargeTypeID. Without property_id, charges are read for the included leases with "
    "embeds=ChargeType, and GET /ChargeTypes is used only when a charge still has no name. "
    "A charge whose ChargeTypeID is not named, and a lease whose current charges all lack "
    "a charge type id and name, are listed on unresolved_charges and left out of the rent "
    "totals. rent_complete is false on that lease. Null rent means neither the tenant nor "
    "the unit was in the charge reads. Zero means the reads returned no charge in effect today."
)
REPORT_PARAMETER_NOTE = (
    "parameter is the token to pass back to rm_run_report. A non-numeric "
    "ReportParameterID is used when Rent Manager sends one; otherwise PropertyName. "
    "The help sample uses ReportParameterID \"0\" plus a sample PropertyName, so this "
    "choice is not confirmed against a live run on this account."
)
SAVED_VALUE_NOTE = (
    "Saved memorized-report values do not include a parameter name in the help "
    "sample, so they are listed separately and not matched to parameters."
)
AGENDA_NOTE = (
    "Appointments are those whose StartDate falls on this date for this user "
    "(AppointmentUsers). Recurring series are not expanded: a series whose start "
    "is outside this date is not listed, even if an occurrence falls today. "
    "Tasks are open items due on this date, plus overdue and undated ones when "
    "those flags are on. Task IsClosed is not a server filter; it is applied here."
)


def _today() -> date:
    return date.today()


def _deps():
    from . import server

    return server


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _require_day(value: str | None, label: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    day = _day(value)
    if day is None:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return day


def _clip(value: Any, limit: int = 800) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _same(left: Any, right: Any) -> bool:
    return left is not None and right is not None and str(left) == str(right)


def _id_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _active_kept(is_active: Any) -> bool:
    """Missing IsActive stays. Only an explicit false is inactive."""
    return is_active is not False


def _sort_lookup(rows: list[dict[str, Any]], name_key: str) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple:
        order = row.get("sort_order")
        try:
            order_n = int(order)
            missing = 0
        except (TypeError, ValueError):
            order_n = 0
            missing = 1
        return (missing, order_n, str(row.get(name_key) or "").casefold())

    return sorted(rows, key=key)


def _parse_which(raw: str | None, order: tuple[str, ...], aliases: dict[str, str], label: str) -> list[str]:
    text = (raw or "all").strip().lower()
    if text in {"", "all", "*"}:
        return list(order)
    chosen: list[str] = []
    bad: list[str] = []
    for part in re.split(r"[,|\s]+", text):
        part = part.strip()
        if not part:
            continue
        if part in {"all", "*"}:
            return list(order)
        key = aliases.get(part)
        if key is None:
            bad.append(part)
        elif key not in chosen:
            chosen.append(key)
    if bad:
        raise ValueError(
            f"Unknown {label}(s): {', '.join(bad)}. Use all or a comma list of {', '.join(order)}."
        )
    if not chosen:
        raise ValueError(f"Pick at least one {label}, or all.")
    return chosen


# Rent Manager uses these as "no date". 0001-01-01 and 1900-01-01 both showed up
# as a move-out and made occupied leases look ended.
_SENTINEL_MOVE_OUT_YEAR = 1900


def _real_day(value: Any) -> date | None:
    """Calendar day, or None for blank and placeholder dates.

    The day is the first 10 characters, so a time and a timezone offset cannot
    move it. A year of 1900 or earlier is not a move-out.
    """
    day = _day(value)
    if day is None or day.year <= _SENTINEL_MOVE_OUT_YEAR:
        return None
    return day


def _lease_has_ended(raw: dict[str, Any], today: date) -> bool:
    """True when a real move-out is today or earlier.

    A future MoveOutDate means notice has been given, not that the lease is over.
    """
    for key in ("MoveOutDate", "DepartureDate"):
        moved = _real_day(raw.get(key))
        if moved is not None and moved <= today:
            return True
    return False


def _lease_is_active(raw: dict[str, Any], today: date | None = None) -> bool:
    return not _lease_has_ended(raw, today or _today())


def _with_property(property_id: int | None, *clauses: str) -> str:
    parts: list[str] = []
    if property_id is not None:
        parts.append(f"PropertyID,eq,{int(property_id)}")
    parts.extend(clauses)
    return ";".join(parts)


def _lease_window_filter(property_id: int | None, start: date, end: date) -> str:
    """Server window: active-renewal end date from today through the horizon.

    PropertyID, when passed, is the first clause. The date clauses are combined
    with ';'. Live reads accept this and return the term window instead of every lease.
    Month-to-month leases are not in this result; see _move_out_filter.
    """
    return _with_property(
        property_id,
        f"ActiveLeaseRenewal.EndDate,ge,{start.isoformat()}",
        f"ActiveLeaseRenewal.EndDate,le,{end.isoformat()}",
    )


def _move_out_filter(field: str, property_id: int | None, start: date, end: date) -> str:
    """Still-occupied move-outs. field is MoveOutDate or ExpectedMoveOutDate.

    ge today and le the horizon, combined with ';'. Both filters were read live.
    A lease whose term already ended, or whose term ends after the horizon, is
    included when its move-out falls in the window.
    """
    return _with_property(
        property_id,
        f"{field},ge,{start.isoformat()}",
        f"{field},le,{end.isoformat()}",
    )


LEASE_FILTER_FALLBACK_NOTE = (
    "Rent Manager rejected the ActiveLeaseRenewal.EndDate window filter. "
    "The lease list was read without that date filter and the window was applied here. "
    "If that download stopped early, the cutoff means this is not every lease in the window."
)
OPEN_ENDED_SERVER_NOTE = (
    "open_ended is not counted. Leases are selected by ActiveLeaseRenewal.EndDate "
    "or by a MoveOutDate or ExpectedMoveOutDate in the window. A lease with no term "
    "end and no move-out in the window is not in those reads, so zero would mean "
    "we did not look, not that none exist."
)
OPEN_ENDED_CUTOFF_NOTE = (
    "open_ended is not counted. The lease download stopped early, so a number would be short."
)


def _renewal_records(lease: dict[str, Any]) -> list[dict[str, Any]]:
    records = _rows(lease.get("LeaseRenewals"))
    seen = {item.get("LeaseRenewalID") for item in records if item.get("LeaseRenewalID") is not None}
    active = lease.get("ActiveLeaseRenewal")
    if isinstance(active, dict):
        ident = active.get("LeaseRenewalID")
        if ident is None or ident not in seen:
            records.append(active)
    return records


def _in_effect(raw: dict[str, Any], today: date) -> bool:
    start = _day(raw.get("StartDate"))
    end = _day(raw.get("EndDate"))
    if start is None and end is None:
        return False
    if start and start > today:
        return False
    if end and end < today:
        return False
    return True


def _current_renewal(records: list[dict[str, Any]], today: date) -> dict[str, Any] | None:
    current = [raw for raw in records if _in_effect(raw, today)]
    if not current:
        return None
    return max(
        current,
        key=lambda raw: (
            _day(raw.get("StartDate")) or date.min,
            raw.get("LeaseRenewalID") or 0,
        ),
    )


def _lease_end(lease: dict[str, Any], current: dict[str, Any] | None) -> date | None:
    if current is not None:
        end = _day(current.get("EndDate"))
        if end is not None:
            return end
    return _day(lease.get("EndDate"))


def _next_renewal(
    records: list[dict[str, Any]],
    current: dict[str, Any] | None,
    lease_end: date | None,
    today: date,
) -> dict[str, Any] | None:
    current_id = None if current is None else current.get("LeaseRenewalID")
    candidates = []
    for raw in records:
        if current_id is not None and raw.get("LeaseRenewalID") == current_id:
            continue
        start = _day(raw.get("StartDate"))
        if lease_end is not None:
            if start is not None and start >= lease_end:
                candidates.append(raw)
        elif start is not None and start > today:
            candidates.append(raw)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda raw: (_day(raw.get("StartDate")) or date.max, raw.get("LeaseRenewalID") or 0),
    )


def _renewal_status(nxt: dict[str, Any] | None) -> str:
    if not nxt:
        return "no_renewal"
    if _day(nxt.get("SignDate")) is not None:
        return "signed"
    return "renewal_on_file"


def _linked(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _rent_for(tenant_raw: dict[str, Any] | None, unit_raw: dict[str, Any] | None, today: date) -> dict[str, Any]:
    if tenant_raw is None and unit_raw is None:
        return _blank_rent()
    srv = _deps()
    batches = (
        srv._charge_rows((tenant_raw or {}).get("RecurringCharges")),
        srv._charge_rows((unit_raw or {}).get("RecurringCharges")),
    )
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[int] = set()
    for batch in batches:
        for raw in batch:
            dto = normalize_recurring_charge(raw, today=today)
            if not dto.get("is_current"):
                continue
            cid = srv._id_of(dto.get("recurring_charge_id"))
            if cid is not None:
                if cid in seen:
                    continue
                seen.add(cid)
            current.append((raw, dto))
    # A payload that includes at least one id or name is a real charge list.
    # A charge with no code is then "other", the same rule as the rent roll.
    # If every current charge lacks both, the read did not carry type data:
    # those amounts are unresolved, not quietly filed as other.
    typed = any(
        srv._recurring_type_name(raw) is not None or srv._recurring_type_id(raw) is not None
        for raw, _dto in current
    )
    merged: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for raw, dto in current:
        type_name = srv._recurring_type_name(raw)
        type_id = srv._recurring_type_id(raw)
        missing_name = type_name is None and (type_id is not None or not typed)
        if missing_name:
            if type_id is not None:
                reason = f"ChargeTypeID {type_id} was not named by GET /ChargeTypes"
            else:
                reason = "Recurring charge had no ChargeTypeID or ChargeType.Name"
            unresolved.append(
                {
                    "recurring_charge_id": dto.get("recurring_charge_id"),
                    "charge_type_id": type_id,
                    "amount": dto.get("amount"),
                    "reason": reason,
                }
            )
            continue
        merged.append(dto)
    buckets = srv._rent_buckets(merged)
    result = {
        "current_rent": buckets["base_rent"],
        "base_rent_tenant": buckets["base_rent_tenant"],
        "base_rent_subsidized": buckets["base_rent_subsidized"],
        "surcharges": buckets["surcharges"],
        "concessions": buckets["concessions"],
        "other_recurring": buckets["other_recurring"],
        "recurring_total": buckets["recurring_total"],
    }
    if unresolved:
        result["rent_complete"] = False
        result["unresolved_charges"] = unresolved
        result["unresolved_amount"] = round(
            sum(float(item["amount"] or 0) for item in unresolved),
            2,
        )
    return result


def _blank_rent() -> dict[str, Any]:
    return {
        "current_rent": None,
        "base_rent_tenant": None,
        "base_rent_subsidized": None,
        "surcharges": None,
        "concessions": None,
        "other_recurring": None,
        "recurring_total": None,
    }


def _expiring_fields(lease: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Dated fields for a lease that has not ended. lease_end is None when there is no term end."""
    if not _lease_is_active(lease, today):
        return None
    records = _renewal_records(lease)
    current = _current_renewal(records, today)
    end = _lease_end(lease, current)
    nxt = _next_renewal(records, current, end, today)
    return {
        "lease_end": end,
        "days_until_end": None if end is None else (end - today).days,
        "renewal_count": len(records),
        "current_renewal_id": None if current is None else current.get("LeaseRenewalID"),
        "renewal_status": _renewal_status(nxt),
        "signable_document_status": None if nxt is None else normalize_lease_renewal(nxt)["signable_document_status"],
        "next_renewal_id": None if nxt is None else nxt.get("LeaseRenewalID"),
        "next_renewal_start": None if nxt is None else normalize_lease_renewal(nxt)["start_date"],
        "next_renewal_end": None if nxt is None else normalize_lease_renewal(nxt)["end_date"],
        "next_renewal_sign_date": None if nxt is None else normalize_lease_renewal(nxt)["sign_date"],
        "records": records,
        "current": current,
        "next": nxt,
    }


def _lease_identity(lease: dict[str, Any]) -> dict[str, Any]:
    tenant = _linked(lease, "Tenant")
    unit = _linked(lease, "Unit")
    prop = _linked(lease, "Property")
    return {
        "lease_id": lease.get("LeaseID"),
        "property_id": lease.get("PropertyID"),
        "property_name": prop.get("Name"),
        "unit_id": lease.get("UnitID") if lease.get("UnitID") is not None else unit.get("UnitID"),
        "unit_name": unit.get("Name"),
        "tenant_id": lease.get("TenantID"),
        "tenant_name": tenant.get("Name"),
        "move_in_date": _day(lease.get("MoveInDate") or lease.get("ArrivalDate")),
        "notice_date": _real_day(lease.get("NoticeDate")),
        "expected_move_out": _real_day(lease.get("ExpectedMoveOutDate")),
        "move_out_date": _real_day(lease.get("MoveOutDate")) or _real_day(lease.get("DepartureDate")),
        "is_active": _lease_is_active(lease),
    }


def _date_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _index(rows: Any, key: str) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            ident = int(row.get(key))
        except (TypeError, ValueError):
            continue
        out[ident] = row
    return out


def _get_all_soft(path: str, **kwargs: Any) -> tuple[Any, str | None]:
    """GET a collection. 404 is an empty list. Other errors are a warning, not a failed tool."""
    srv = _deps()
    try:
        return srv.client().get_all(path, **kwargs), None
    except RMError as exc:
        if exc.status == 404:
            return PageList([], truncated=False, total=0), None
        return PageList([], truncated=False, total=None), f"{path} was not read ({exc.status})."


def _read_one(path: str) -> dict[str, Any] | None:
    """GET one resource. 404 and an empty body are None."""
    srv = _deps()
    try:
        response = srv.client().request("GET", path)
    except RMError as exc:
        if exc.status == 404:
            return None
        raise
    if not response.content:
        return None
    payload = response.json()
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if not isinstance(payload, dict):
        return None
    return payload


def search_prospects(
    property_id: int | None = None,
    query: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    lead_source: str | None = None,
    lead_source_id: int | None = None,
    date_on: str = "created",
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> str:
    srv = _deps()
    try:
        which = (date_on or "created").strip().lower()
        if which in {"create", "created", "create_date", "created_date"}:
            date_field = "created"
        elif which in {"first", "first_contact", "contact", "firstcontact"}:
            date_field = "first_contact"
        else:
            raise ValueError("date_on must be created or first_contact")
        start = _require_day(date_from, "date_from")
        end = _require_day(date_to, "date_to")
        if start and end and start > end:
            raise ValueError("date_from is after date_to")
        filters = None
        if property_id is not None:
            filters = f"PropertyID,eq,{int(property_id)}"
        rows = srv.client().get_all(
            "/Prospects",
            filters=filters,
            embeds=PROSPECT_LIST_EMBEDS,
            pagesize=200,
            max_pages=8,
        )
        needle = (query or "").strip().casefold()
        stage_text = (stage or "").strip()
        status_text = (status or "").strip().casefold()
        source_text = (lead_source or "").strip().casefold()
        full = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            dto = normalize_prospect_card(raw)
            if needle:
                hay = " ".join(
                    str(dto.get(key) or "")
                    for key in ("name", "first_name", "last_name", "prospect_id", "display_id", "email", "phone")
                ).casefold()
                if needle not in hay:
                    continue
            if stage_text:
                if stage_text.isdigit():
                    if not _same(dto.get("stage_id"), int(stage_text)):
                        continue
                elif stage_text.casefold() != str(dto.get("stage_name") or "").casefold():
                    continue
            if status_text and status_text != str(dto.get("status") or "").casefold():
                continue
            if lead_source_id is not None and not _same(dto.get("lead_source_id"), int(lead_source_id)):
                continue
            if source_text and source_text not in str(dto.get("lead_source") or "").casefold():
                continue
            if start or end:
                stamp = _day(dto.get(date_field))
                if stamp is None:
                    continue
                if start and stamp < start:
                    continue
                if end and stamp > end:
                    continue
            dto["comment"] = _clip(dto.get("comment"), 300)
            full.append(dto)
        dated = [row for row in full if row.get(date_field)]
        undated = [row for row in full if not row.get(date_field)]
        dated.sort(key=lambda row: str(row.get("name") or "").casefold())
        dated.sort(key=lambda row: str(row.get(date_field)), reverse=True)
        undated.sort(key=lambda row: str(row.get("name") or "").casefold())
        full = dated + undated
        cap = srv._clamp(limit, 1, 200)
        data = full[:cap]
        return srv._ok(
            data,
            **srv._cutoff_meta(
                rows,
                len(data),
                row_capped=len(full) > cap,
                count=len(data),
                property_id=property_id,
                query=query,
                stage=stage,
                status=status,
                lead_source=lead_source,
                lead_source_id=lead_source_id,
                date_on=date_field,
                date_from=None if start is None else start.isoformat(),
                date_to=None if end is None else end.isoformat(),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def prospect_detail(prospect_id: int) -> str:
    srv = _deps()
    try:
        pid = int(prospect_id)
        rows = srv.client().get_all(
            "/Prospects",
            filters=f"ProspectID,eq,{pid}",
            embeds=PROSPECT_DETAIL_EMBEDS,
            pagesize=5,
            max_pages=1,
        )
        raw = next((row for row in rows if isinstance(row, dict)), None)
        if raw is None:
            return srv._ok(None, found=False, prospect_id=pid)
        card = normalize_prospect_card(raw)
        contacts = [normalize_prospect_contact(item) for item in _rows(raw.get("Contacts"))]
        if not contacts and isinstance(raw.get("PrimaryContact"), dict):
            contacts = [normalize_prospect_contact(raw["PrimaryContact"])]
        addresses = [normalize_prospect_address(item) for item in _rows(raw.get("Addresses"))]
        history_rows, history_warning = _get_all_soft(
            f"/Prospects/{pid}/ProspectStageHistory",
            embeds="ProspectStage",
            pagesize=100,
            max_pages=5,
        )
        rental_rows, rental_warning = _get_all_soft(
            f"/Prospects/{pid}/InterestedRentals",
            embeds="Unit,Property",
            pagesize=100,
            max_pages=3,
        )
        warnings = [note for note in (history_warning, rental_warning) if note]
        history = [normalize_stage_history(item) for item in history_rows if isinstance(item, dict)]
        history.sort(key=lambda row: (row.get("from_date") or "", row.get("history_id") or 0), reverse=True)
        interested = [normalize_interested_rental(item) for item in rental_rows if isinstance(item, dict)]
        interested.sort(key=lambda row: (str(row.get("unit_name") or ""), row.get("unit_id") or 0))
        lost = normalize_lost_reason(
            raw.get("ProspectLostReason") if isinstance(raw.get("ProspectLostReason"), dict) else None,
            fallback_id=raw.get("ProspectLostReasonID"),
            fallback_description=raw.get("ProspectLostReasonDescription"),
        )
        stage = raw.get("CurrentProspectStage") if isinstance(raw.get("CurrentProspectStage"), dict) else {}
        card.update(
            {
                "contacts": contacts,
                "addresses": addresses,
                "interested_units": interested,
                "stage_history": history,
                "lost_reason": lost,
                "stage_description": stage.get("Description") or None,
                "expected_lease_start": _date_iso(_day(raw.get("ExpectedLeaseStart"))),
                "expected_lease_end": _date_iso(_day(raw.get("ExpectedLeaseEnd"))),
            }
        )
        source = srv._merged_source(rows, history_rows, rental_rows)
        return srv._ok(
            card,
            **srv._cutoff_meta(
                source,
                len(history) + len(interested),
                found=True,
                prospect_id=pid,
                contact_count=len(contacts),
                history_count=len(history),
                interested_count=len(interested),
                warnings=warnings or None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


_LOOKUP_ORDER = ("stages", "lost_reasons", "lead_sources")
_LOOKUP_ALIASES = {
    "stage": "stages",
    "stages": "stages",
    "prospect_stage": "stages",
    "prospect_stages": "stages",
    "lost": "lost_reasons",
    "lost_reason": "lost_reasons",
    "lost_reasons": "lost_reasons",
    "lead": "lead_sources",
    "lead_source": "lead_sources",
    "lead_sources": "lead_sources",
    "source": "lead_sources",
    "sources": "lead_sources",
}
_LOOKUP_PATHS = {
    "stages": "/ProspectStages",
    "lost_reasons": "/ProspectLostReasons",
    "lead_sources": "/LeadSources",
}


def prospect_lookups(which: str = "all", active_only: bool = True, limit: int = 200) -> str:
    srv = _deps()
    try:
        selected = _parse_which(which, _LOOKUP_ORDER, _LOOKUP_ALIASES, "lookup")
        cap = srv._clamp(limit, 1, 500)
        builders = {
            "stages": normalize_prospect_stage,
            "lost_reasons": normalize_lost_reason_lookup,
            "lead_sources": normalize_lead_source,
        }
        name_keys = {"stages": "name", "lost_reasons": "name", "lead_sources": "name"}
        data: dict[str, list[dict[str, Any]]] = {}
        views = []
        row_capped = False
        for key in selected:
            rows = srv.client().get_all(_LOOKUP_PATHS[key], pagesize=200, max_pages=4)
            views.append(rows)
            built = []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                dto = builders[key](raw)
                if active_only and not _active_kept(dto.get("is_active")):
                    continue
                built.append(dto)
            built = _sort_lookup(built, name_keys[key])
            if len(built) > cap:
                row_capped = True
                built = built[:cap]
            data[key] = built
        source = srv._merged_source(*views) if views else srv._FetchView(False, None)
        return srv._ok(
            data,
            **srv._cutoff_meta(
                source,
                sum(len(items) for items in data.values()),
                row_capped=row_capped,
                which=selected,
                active_only=bool(active_only),
                counts={key: len(items) for key, items in data.items()},
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _fetch_property_charges(property_id: int) -> tuple[Any, Any]:
    srv = _deps()
    filters = f"PropertyID,eq,{int(property_id)}"
    tenants = srv.client().get_all(
        "/Tenants",
        filters=filters,
        embeds=srv._TENANT_RENT_EMBEDS,
        pagesize=200,
        max_pages=10,
    )
    units = srv.client().get_all(
        "/Units",
        filters=filters,
        embeds=srv._UNIT_RENT_EMBEDS,
        pagesize=200,
        max_pages=10,
    )
    return tenants, units


def _future_move_out_day(raw: dict[str, Any], today: date, window_end: date) -> date | None:
    """Earliest real MoveOutDate or ExpectedMoveOutDate after today and on or before the horizon."""
    found: list[date] = []
    for key in ("MoveOutDate", "ExpectedMoveOutDate"):
        day = _real_day(raw.get(key))
        if day is not None and today < day <= window_end:
            found.append(day)
    return min(found) if found else None


def _in_move_window(day: date | None, today: date, window_end: date) -> bool:
    return day is not None and today < day <= window_end


def _read_leases(filters: str | None) -> Any:
    return _deps().client().get_all(
        "/Leases",
        filters=filters,
        embeds=LEASE_LIST_EMBEDS,
        pagesize=200,
        max_pages=10,
    )


def _occupancy_notice_unit_ids(units: Any, today: date, window_end: date) -> list[int]:
    """Units whose occupancy move-out, or a notice with no later move-out, is in the window."""
    found: list[int] = []
    seen: set[int] = set()
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        occ = unit.get("CurrentOccupancyStatus")
        if isinstance(occ, list):
            occ = next((item for item in occ if isinstance(item, dict)), None)
        if not isinstance(occ, dict):
            occ = {}
        expected = _real_day(occ.get("ExpectedMoveOutDate"))
        notice = _real_day(occ.get("NoticeDate"))
        in_window = _in_move_window(expected, today, window_end)
        # A notice with no expected date is included. A move-out after the horizon is not.
        if in_window or (notice is not None and expected is None):
            try:
                unit_id = int(unit.get("UnitID"))
            except (TypeError, ValueError):
                continue
            if unit_id not in seen:
                seen.add(unit_id)
                found.append(unit_id)
    return found


def _leases_for_units(unit_ids: list[int]) -> list[Any]:
    """GET /Leases by UnitID,in chunks. A 400 on a chunk falls back to that chunk's units, capped."""
    views: list[Any] = []
    if not unit_ids:
        return views
    srv = _deps()
    for start in range(0, len(unit_ids), 50):
        chunk = unit_ids[start : start + 50]
        joined = ",".join(str(unit_id) for unit_id in chunk)
        try:
            views.append(
                _read_leases(f"UnitID,in,({joined})")
            )
        except RMError as exc:
            if exc.status != 400:
                raise
            for unit_id in chunk[:40]:
                views.append(_read_leases(f"UnitID,eq,{unit_id}"))
            if len(chunk) > 40:
                views.append(PageList([], truncated=True, total=len(chunk)))
    return views


def _notice_leases_from_units(property_id: int | None, today: date, window_end: date) -> list[Any]:
    """Fallback when a move-out filter is rejected. Uses CurrentOccupancyStatus."""
    if property_id is not None:
        filters = f"PropertyID,eq,{int(property_id)}"
    else:
        filters = "Property.IsActive,eq,true"
    units = _deps().client().get_all(
        "/Units",
        filters=filters,
        embeds="CurrentOccupancyStatus",
        pagesize=200,
        max_pages=10,
    )
    return [units, *_leases_for_units(_occupancy_notice_unit_ids(units, today, window_end))]


def _fetch_notice_lease_views(property_id: int | None, today: date, window_end: date) -> list[Any]:
    """Move-out reads. A 400 on either date field uses the unit occupancy fallback once."""
    views: list[Any] = []
    rejected = False
    for field in ("MoveOutDate", "ExpectedMoveOutDate"):
        try:
            views.append(_read_leases(_move_out_filter(field, property_id, today, window_end)))
        except RMError as exc:
            if exc.status != 400:
                raise
            rejected = True
    if rejected:
        views.extend(_notice_leases_from_units(property_id, today, window_end))
    return views


def _lease_key(raw: dict[str, Any]) -> Any:
    try:
        return int(raw.get("LeaseID"))
    except (TypeError, ValueError):
        return id(raw)


def _dedupe_leases(views: list[Any]) -> list[dict[str, Any]]:
    """First copy wins, so the term-window row is kept when a move-out read repeats it."""
    chosen: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for view in views:
        for raw in view or []:
            if not isinstance(raw, dict):
                continue
            key = _lease_key(raw)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(raw)
    return chosen


def _fetch_parent_charges(kind: str, parent_id: int) -> Any:
    """GET /Tenants|Units/{id}/RecurringCharges?embeds=ChargeType.

    Errors propagate. A 403 or 404 must not become an empty charge list.
    """
    srv = _deps()
    path = f"/{kind}/{int(parent_id)}/RecurringCharges"
    return srv.client().get_all(path, embeds="ChargeType", pagesize=100, max_pages=5)


def _collect_expiring_leases(
    property_id: int | None,
    within_days: int | None,
    limit: int,
) -> dict[str, Any]:
    """Active leases whose term ends inside the window, with current_rent attached.

    Shared by rm_leases_expiring and rm_renewal_pipeline so the window,
    renewal_status, and RC+RC8 rent stay one implementation. Raises on API
    errors. The row cap is the same 1..200 range as rm_leases_expiring.
    """
    srv = _deps()
    window = srv._clamp(90 if within_days is None else int(within_days), 0, 366)
    cap = srv._clamp(limit, 1, 200)
    today = _today()
    window_end = today + timedelta(days=window)
    server_filters = _lease_window_filter(property_id, today, window_end)
    filter_mode = "server"
    try:
        window_rows = _read_leases(server_filters)
    except RMError as exc:
        # A rejected date filter is a 400. Permission errors still propagate.
        if exc.status != 400:
            raise
        filter_mode = "client"
        fallback = f"PropertyID,eq,{int(property_id)}" if property_id is not None else None
        window_rows = _read_leases(fallback)
    lease_views: list[Any] = [window_rows]
    if filter_mode == "server":
        # The term filter cannot see month-to-month or a later term with notice.
        lease_views.extend(_fetch_notice_lease_views(property_id, today, window_end))
    count_open_ended = filter_mode == "client" and not getattr(window_rows, "truncated", False)
    open_ended = 0
    pending = []
    for raw in _dedupe_leases(lease_views):
        fields = _expiring_fields(raw, today)
        if fields is None:
            continue
        days_left = fields["days_until_end"]
        in_window = days_left is not None and 0 <= days_left <= window
        move_out = _future_move_out_day(raw, today, window_end)
        if not in_window and move_out is None:
            if days_left is None and count_open_ended:
                open_ended += 1
            continue
        pending.append((raw, fields, in_window, move_out))
    pending.sort(
        key=lambda item: (
            0 if item[2] else 1,
            item[1]["days_until_end"] if item[2] else (item[3] - today).days,
            str(_linked(item[0], "Unit").get("Name") or ""),
            item[0].get("LeaseID") or 0,
        )
    )
    row_capped = len(pending) > cap
    chosen = [(raw, fields) for raw, fields, _in_window, _move in pending[:cap]]
    charge_views = []
    charge_mode = "per_lease"
    tenants_by_id: dict[int, dict[str, Any]] = {}
    units_by_id: dict[int, dict[str, Any]] = {}
    if property_id is not None and chosen:
        tenant_rows, unit_rows = _fetch_property_charges(int(property_id))
        charge_views.extend([tenant_rows, unit_rows])
        tenants_by_id = _index(tenant_rows, "TenantID")
        units_by_id = _index(unit_rows, "UnitID")
        charge_mode = "property_collections"
    elif chosen:
        seen_tenants: set[int] = set()
        seen_units: set[int] = set()
        for raw, _fields in chosen:
            try:
                tid = int(raw.get("TenantID"))
            except (TypeError, ValueError):
                tid = None
            try:
                uid = int(raw.get("UnitID"))
            except (TypeError, ValueError):
                uid = None
            if tid is not None and tid not in seen_tenants:
                seen_tenants.add(tid)
                fetched = _fetch_parent_charges("Tenants", tid)
                charge_views.append(fetched)
                tenants_by_id[tid] = {"TenantID": tid, "RecurringCharges": list(fetched)}
            if uid is not None and uid not in seen_units:
                seen_units.add(uid)
                fetched = _fetch_parent_charges("Units", uid)
                charge_views.append(fetched)
                units_by_id[uid] = {"UnitID": uid, "RecurringCharges": list(fetched)}
    if tenants_by_id or units_by_id:
        catalog = srv._load_charge_type_catalog(
            list(tenants_by_id.values()),
            list(units_by_id.values()),
        )
        if catalog is not None:
            charge_views.append(catalog)
    data = []
    for raw, fields in chosen:
        identity = _lease_identity(raw)
        try:
            tid = int(identity["tenant_id"]) if identity["tenant_id"] is not None else None
        except (TypeError, ValueError):
            tid = None
        try:
            uid = int(identity["unit_id"]) if identity["unit_id"] is not None else None
        except (TypeError, ValueError):
            uid = None
        rent = _rent_for(
            tenants_by_id.get(tid) if tid is not None else None,
            units_by_id.get(uid) if uid is not None else None,
            today,
        )
        data.append(
            {
                **{key: (_date_iso(value) if isinstance(value, date) else value) for key, value in identity.items()},
                "lease_end": None if fields["lease_end"] is None else fields["lease_end"].isoformat(),
                "days_until_end": fields["days_until_end"],
                "renewal_count": fields["renewal_count"],
                "current_renewal_id": fields["current_renewal_id"],
                "renewal_status": fields["renewal_status"],
                "signable_document_status": fields["signable_document_status"],
                "next_renewal_id": fields["next_renewal_id"],
                "next_renewal_start": fields["next_renewal_start"],
                "next_renewal_end": fields["next_renewal_end"],
                "next_renewal_sign_date": fields["next_renewal_sign_date"],
                **rent,
            }
        )
    return {
        "data": data,
        "lease_views": lease_views,
        "charge_views": charge_views,
        "row_capped": row_capped,
        "open_ended": open_ended if count_open_ended else None,
        "open_ended_note": None if count_open_ended else (
            OPEN_ENDED_CUTOFF_NOTE if filter_mode == "client" else OPEN_ENDED_SERVER_NOTE
        ),
        "window": window,
        "window_end": window_end,
        "charge_mode": charge_mode,
        "property_id": property_id,
        "filter_mode": filter_mode,
    }


def _lease_filter_meta(collected: dict[str, Any]) -> dict[str, Any]:
    """How the lease list was filtered. A missing open_ended count is never a fake zero."""
    meta: dict[str, Any] = {
        "lease_filter": collected["filter_mode"],
        "open_ended": collected["open_ended"],
    }
    if collected.get("open_ended_note"):
        meta["open_ended_note"] = collected["open_ended_note"]
    if collected["filter_mode"] == "client":
        meta["lease_filter_note"] = LEASE_FILTER_FALLBACK_NOTE
    return meta


def _expiring_notice(source: Any, collected: dict[str, Any], *, pipeline: bool) -> str | None:
    cut = bool(getattr(source, "truncated", False) or collected["row_capped"])
    if not cut:
        return None
    if pipeline:
        text = (
            "CUT OFF: this is not every expiring lease in the window. The lease, charge, "
            "unit, property, or offer download stopped early, or the row limit was hit. "
            "Counts describe the leases included here, not the ones left out."
        )
    else:
        text = (
            "CUT OFF: this is not every expiring lease. The lease or charge download "
            "stopped early, or the row limit was hit."
        )
    if collected["filter_mode"] == "client":
        text = f"{text} {LEASE_FILTER_FALLBACK_NOTE}"
    return text


def _rent_gap(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Meta for leases whose current charges could not all be classified."""
    incomplete = sum(1 for row in rows if row.get("rent_complete") is False)
    if not incomplete:
        return {}
    return {
        "rent_complete": False,
        "rent_notice": (
            f"Rent is incomplete for {incomplete} lease(s). "
            "Charges that could not be classified are on unresolved_charges "
            "and are not included in the rent totals."
        ),
    }


def leases_expiring(property_id: int | None = None, within_days: int = 90, limit: int = 50) -> str:
    srv = _deps()
    try:
        collected = _collect_expiring_leases(property_id, within_days, limit)
        data = collected["data"]
        source = srv._merged_source(*collected["lease_views"], *collected["charge_views"])
        return srv._ok(
            data,
            **srv._cutoff_meta(
                source,
                len(data),
                row_capped=collected["row_capped"],
                notice=_expiring_notice(source, collected, pipeline=False),
                count=len(data),
                property_id=property_id,
                within_days=collected["window"],
                charge_mode=collected["charge_mode"],
                renewal_note=RENEWAL_NOTE,
                rent_note=RENT_NOTE,
                **_lease_filter_meta(collected),
                **_rent_gap(data),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _lease_detail_dto(raw: dict[str, Any], today: date, *, with_offers: bool) -> dict[str, Any]:
    fields = _expiring_fields(raw, today) if _lease_is_active(raw, today) else None
    records = _renewal_records(raw) if fields is None else fields["records"]
    if fields is None:
        current = _current_renewal(records, today)
        end = _lease_end(raw, current)
        nxt = _next_renewal(records, current, end, today)
        fields = {
            "lease_end": end,
            "days_until_end": None if end is None else (end - today).days,
            "renewal_count": len(records),
            "current_renewal_id": None if current is None else current.get("LeaseRenewalID"),
            "renewal_status": _renewal_status(nxt),
            "signable_document_status": None if nxt is None else normalize_lease_renewal(nxt)["signable_document_status"],
            "next_renewal_id": None if nxt is None else nxt.get("LeaseRenewalID"),
            "current": current,
            "next": nxt,
        }
    current_id = fields["current_renewal_id"]
    next_id = fields["next_renewal_id"]
    active = raw.get("ActiveLeaseRenewal") if isinstance(raw.get("ActiveLeaseRenewal"), dict) else {}
    renewals = []
    offer_calls = 0
    for record in records[:20]:
        dto = normalize_lease_renewal(record)
        dto["is_current"] = _same(dto.get("lease_renewal_id"), current_id)
        dto["is_next"] = _same(dto.get("lease_renewal_id"), next_id)
        offer = None
        if with_offers and dto.get("lease_renewal_id") is not None:
            offer_calls += 1
            offer = normalize_increase_offer(_read_one(f"/LeaseRenewals/{int(dto['lease_renewal_id'])}/LeaseRenewalIncreaseOffer"))
        dto["increase_offer"] = offer
        renewals.append(dto)
    identity = _lease_identity(raw)
    return {
        **{key: (_date_iso(value) if isinstance(value, date) else value) for key, value in identity.items()},
        "lease_end": None if fields["lease_end"] is None else fields["lease_end"].isoformat(),
        "days_until_end": fields["days_until_end"],
        "renewal_count": len(records),
        "renewal_status": fields["renewal_status"],
        "signable_document_status": fields["signable_document_status"],
        "current_renewal_id": current_id,
        "active_lease_renewal_id": active.get("LeaseRenewalID"),
        "renewals": renewals,
        "offers_truncated": len(records) > 20,
        "_offer_calls": offer_calls,
    }


def lease_renewal_detail(lease_id: int | None = None, tenant_id: int | None = None) -> str:
    srv = _deps()
    try:
        if lease_id is None and tenant_id is None:
            raise ValueError("Pass lease_id or tenant_id")
        filters = f"LeaseID,eq,{int(lease_id)}" if lease_id is not None else f"TenantID,eq,{int(tenant_id)}"
        rows = srv.client().get_all(
            "/Leases",
            filters=filters,
            embeds=LEASE_DETAIL_EMBEDS,
            pagesize=50,
            max_pages=2,
        )
        today = _today()
        leases = []
        offer_calls = 0
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if tenant_id is not None and not _same(raw.get("TenantID"), int(tenant_id)):
                continue
            if lease_id is not None and not _same(raw.get("LeaseID"), int(lease_id)):
                continue
            dto = _lease_detail_dto(raw, today, with_offers=True)
            offer_calls += int(dto.pop("_offer_calls", 0))
            leases.append(dto)
        cap = 10
        row_capped = len(leases) > cap
        leases = leases[:cap]
        return srv._ok(
            {"leases": leases},
            **srv._cutoff_meta(
                rows,
                len(leases),
                row_capped=row_capped,
                found=bool(leases),
                count=len(leases),
                lease_id=lease_id,
                tenant_id=tenant_id,
                offer_requests=offer_calls,
                renewal_note=RENEWAL_NOTE,
                offer_note=(
                    "increase_offer is GET /LeaseRenewals/{id}/LeaseRenewalIncreaseOffer. "
                    "A 404 means no offer. offer_status is inferred: approved when ApprovalDate "
                    "is set, otherwise set. The offer model has no status field."
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _appointment_at_property(raw: dict[str, Any], property_id: int) -> bool:
    """True when the linked tenant or the linked prospect is at this property."""
    for key in ("Tenant", "Prospect"):
        linked = raw.get(key)
        if isinstance(linked, dict) and _same(linked.get("PropertyID"), property_id):
            return True
    return False


def _appointment_on_day(raw: dict[str, Any], start: date, end: date) -> bool:
    start_day = _day(raw.get("StartDate"))
    return bool(start_day and start <= start_day <= end)


def list_appointments(
    date_from: str | None = None,
    date_to: str | None = None,
    user_id: int | None = None,
    property_id: int | None = None,
    prospect_id: int | None = None,
    tenant_id: int | None = None,
    limit: int = 50,
) -> str:
    srv = _deps()
    try:
        today = _today()
        start = _require_day(date_from, "date_from") or today
        end = _require_day(date_to, "date_to") or start
        if date_from and not date_to:
            end = start
        if date_to and not date_from:
            start = end
        if start > end:
            raise ValueError("date_from is after date_to")
        if (end - start).days > _APPOINTMENT_WINDOW_DAYS:
            raise ValueError(f"Appointment window is capped at {_APPOINTMENT_WINDOW_DAYS} days")
        rows = srv.client().get_all(
            "/Appointments",
            embeds=APPOINTMENT_EMBEDS,
            pagesize=200,
            max_pages=8,
        )
        full = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if not _appointment_on_day(raw, start, end):
                continue
            dto = normalize_appointment(raw)
            if user_id is not None and not any(_same(item, int(user_id)) for item in _id_list(dto.get("assigned_user_ids"))):
                continue
            if property_id is not None and not _appointment_at_property(raw, int(property_id)):
                continue
            if prospect_id is not None and not _same(dto.get("prospect_id"), int(prospect_id)):
                continue
            if tenant_id is not None and not _same(dto.get("tenant_id"), int(tenant_id)):
                continue
            dto["description"] = _clip(dto.get("description"))
            full.append(dto)
        full.sort(key=lambda row: (row.get("starts_at") or "", row.get("appointment_id") or 0))
        cap = srv._clamp(limit, 1, 200)
        data = full[:cap]
        notice = None
        if getattr(rows, "truncated", False) or len(full) > cap:
            notice = (
                "CUT OFF: appointments are downloaded without a date filter (Rent Manager's "
                "appointment filters do not cover StartDate or the assigned user), then filtered "
                "here. A page cap or the row limit may have hidden some."
            )
        return srv._ok(
            data,
            **srv._cutoff_meta(
                rows,
                len(data),
                row_capped=len(full) > cap,
                notice=notice,
                count=len(data),
                date_from=start.isoformat(),
                date_to=end.isoformat(),
                user_id=user_id,
                property_id=property_id,
                prospect_id=prospect_id,
                tenant_id=tenant_id,
                recurrence_note=(
                    "Rows are included when StartDate falls in the window. "
                    "Recurring series are not expanded."
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _task_matches(
    dto: dict[str, Any],
    *,
    include_closed: bool,
    overdue_only: bool,
    due_from: date | None,
    due_to: date | None,
    prospect_id: int | None,
    tenant_id: int | None,
    agenda_day: date | None,
    include_overdue: bool,
    include_undated: bool,
) -> bool:
    if agenda_day is not None:
        if dto.get("is_closed"):
            return False
        due = dto.get("due_date")
        if due == agenda_day.isoformat():
            return True
        if include_overdue and dto.get("is_overdue"):
            return True
        if include_undated and not due:
            return True
        return False
    if not include_closed and dto.get("is_closed"):
        return False
    if overdue_only and not dto.get("is_overdue"):
        return False
    if due_from or due_to:
        due = _day(dto.get("due_date"))
        if due is None:
            return False
        if due_from and due < due_from:
            return False
        if due_to and due > due_to:
            return False
    if prospect_id is not None and not _same(dto.get("prospect_id"), prospect_id):
        return False
    if tenant_id is not None and not _same(dto.get("tenant_id"), tenant_id):
        return False
    return True


def list_tasks(
    user_id: int | None = None,
    prospect_id: int | None = None,
    tenant_id: int | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    overdue_only: bool = False,
    include_closed: bool = False,
    limit: int = 50,
    agenda_day: date | None = None,
    include_overdue: bool = True,
    include_undated: bool = True,
) -> tuple[list[dict[str, Any]], Any, bool]:
    """Return (rows, page source, row_capped). Raises ValueError on bad dates."""
    srv = _deps()
    start = _require_day(due_from, "due_from")
    end = _require_day(due_to, "due_to")
    if start and end and start > end:
        raise ValueError("due_from is after due_to")
    if user_id is not None:
        path = f"/Users/{int(user_id)}/Tasks"
    else:
        path = "/Tasks"
    rows = srv.client().get_all(path, embeds=TASK_EMBEDS, pagesize=200, max_pages=8)
    today = _today()
    full = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        dto = normalize_task(raw, today=today)
        if not _task_matches(
            dto,
            include_closed=include_closed,
            overdue_only=overdue_only,
            due_from=start,
            due_to=end,
            prospect_id=None if prospect_id is None else int(prospect_id),
            tenant_id=None if tenant_id is None else int(tenant_id),
            agenda_day=agenda_day,
            include_overdue=include_overdue,
            include_undated=include_undated,
        ):
            continue
        dto["description"] = _clip(dto.get("description"))
        full.append(dto)

    def sort_key(row: dict[str, Any]) -> tuple:
        if agenda_day is not None:
            if row.get("is_overdue"):
                band = 0
            elif row.get("due_date") == agenda_day.isoformat():
                band = 1
            else:
                band = 2
        else:
            band = 0 if row.get("is_overdue") else 1
        due = row.get("due_date") or "9999-99-99"
        return (band, due, row.get("task_id") or 0)

    full.sort(key=sort_key)
    cap = srv._clamp(limit, 1, 200)
    return full[:cap], rows, len(full) > cap


def open_tasks(
    user_id: int | None = None,
    prospect_id: int | None = None,
    tenant_id: int | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    overdue_only: bool = False,
    include_closed: bool = False,
    limit: int = 50,
) -> str:
    srv = _deps()
    try:
        data, rows, row_capped = list_tasks(
            user_id=user_id,
            prospect_id=prospect_id,
            tenant_id=tenant_id,
            due_from=due_from,
            due_to=due_to,
            overdue_only=overdue_only,
            include_closed=include_closed,
            limit=limit,
        )
        notice = None
        if getattr(rows, "truncated", False) or row_capped:
            notice = (
                "CUT OFF: open tasks are filtered in Python. IsClosed and DueDate are not "
                "sent as a Rent Manager filter. A page cap or the row limit may have hidden some."
            )
        return srv._ok(
            data,
            **srv._cutoff_meta(
                rows,
                len(data),
                row_capped=row_capped,
                notice=notice,
                count=len(data),
                user_id=user_id,
                prospect_id=prospect_id,
                tenant_id=tenant_id,
                due_from=due_from,
                due_to=due_to,
                overdue_only=bool(overdue_only),
                include_closed=bool(include_closed),
                source_path=(f"/Users/{int(user_id)}/Tasks" if user_id is not None else "/Tasks"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def staff_agenda(
    user_id: int,
    on_date: str | None = None,
    include_overdue_tasks: bool = True,
    include_undated_tasks: bool = True,
    limit: int = 50,
) -> str:
    srv = _deps()
    try:
        day = _require_day(on_date, "on_date") or _today()
        uid = int(user_id)
        appointments = json.loads(
            list_appointments(
                date_from=day.isoformat(),
                date_to=day.isoformat(),
                user_id=uid,
                limit=limit,
            )
        )
        if not appointments.get("ok"):
            return json.dumps(appointments)
        tasks, task_rows, task_capped = list_tasks(
            user_id=uid,
            agenda_day=day,
            include_overdue=bool(include_overdue_tasks),
            include_undated=bool(include_undated_tasks),
            limit=limit,
        )
        appt_meta = appointments.get("meta") or {}
        truncated = bool(appt_meta.get("truncated") or getattr(task_rows, "truncated", False) or task_capped)
        notice = None
        if truncated:
            notice = (
                "CUT OFF: the agenda is not complete. An appointment or task download stopped "
                "early, or a row limit was hit."
            )
        return srv._ok(
            {
                "user_id": uid,
                "date": day.isoformat(),
                "appointments": appointments.get("data") or [],
                "tasks": tasks,
            },
            truncated=truncated,
            total=None,
            notice=notice,
            appointment_count=len(appointments.get("data") or []),
            task_count=len(tasks),
            include_overdue_tasks=bool(include_overdue_tasks),
            include_undated_tasks=bool(include_undated_tasks),
            agenda_note=AGENDA_NOTE,
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def list_users(query: str | None = None, active_only: bool = True, limit: int = 50) -> str:
    srv = _deps()
    try:
        rows = srv.client().get_all("/Users", pagesize=100, max_pages=5)
        needle = (query or "").strip().casefold()
        full = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            dto = normalize_user_brief(raw)
            if active_only and not _active_kept(dto.get("is_active")):
                continue
            if needle:
                hay = " ".join(
                    str(dto.get(key) or "")
                    for key in ("name", "username", "email", "first_name", "last_name")
                ).casefold()
                if needle not in hay:
                    continue
            full.append(dto)
        full.sort(key=lambda row: (str(row.get("name") or "").casefold(), row.get("user_id") or 0))
        cap = srv._clamp(limit, 1, 200)
        data = full[:cap]
        return srv._ok(
            data,
            **srv._cutoff_meta(
                rows,
                len(data),
                row_capped=len(full) > cap,
                count=len(data),
                query=query,
                active_only=bool(active_only),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


_REPORT_KINDS = ("report", "memorized")
_REPORT_KIND_ALIASES = {
    "report": "report",
    "reports": "report",
    "memorized": "memorized",
    "memorized_report": "memorized",
    "memorized_reports": "memorized",
}


def list_reports(query: str | None = None, kind: str = "all", limit: int = 100) -> str:
    srv = _deps()
    try:
        selected = _parse_which(kind, _REPORT_KINDS, _REPORT_KIND_ALIASES, "report kind")
        cap = srv._clamp(limit, 1, 300)
        needle = (query or "").strip().casefold()
        data: list[dict[str, Any]] = []
        views = []
        row_capped = False
        counts: dict[str, int] = {}
        if "report" in selected:
            rows = srv.client().get_all("/Reports", embeds=REPORT_LIST_EMBEDS, pagesize=200, max_pages=5)
            views.append(rows)
            built = []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                dto = normalize_report_listing(raw)
                hay = f"{dto.get('name') or ''} {dto.get('description') or ''} {dto.get('category_name') or ''}".casefold()
                if needle and needle not in hay:
                    continue
                built.append(dto)
            built.sort(key=lambda row: (str(row.get("category_name") or "").casefold(), str(row.get("name") or "").casefold()))
            if len(built) > cap:
                row_capped = True
                built = built[:cap]
            counts["report"] = len(built)
            data.extend(built)
        if "memorized" in selected:
            rows = srv.client().get_all(
                "/MemorizedReports",
                embeds=MEMORIZED_LIST_EMBEDS,
                pagesize=200,
                max_pages=5,
            )
            views.append(rows)
            built = []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                dto = normalize_memorized_report(raw)
                hay = f"{dto.get('name') or ''} {dto.get('category_name') or ''}".casefold()
                if needle and needle not in hay:
                    continue
                built.append(dto)
            built.sort(key=lambda row: (str(row.get("category_name") or "").casefold(), str(row.get("name") or "").casefold()))
            if len(built) > cap:
                row_capped = True
                built = built[:cap]
            counts["memorized"] = len(built)
            data.extend(built)
        source = srv._merged_source(*views) if views else srv._FetchView(False, None)
        return srv._ok(
            data,
            **srv._cutoff_meta(
                source,
                len(data),
                row_capped=row_capped,
                count=len(data),
                counts=counts,
                kind=selected,
                query=query,
                per_kind_limit=cap,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def report_parameters(report_id: int | None = None, memorized_report_id: int | None = None) -> str:
    srv = _deps()
    try:
        if report_id is None and memorized_report_id is None:
            raise ValueError("Pass report_id or memorized_report_id")
        if report_id is not None and memorized_report_id is not None:
            raise ValueError("Pass report_id or memorized_report_id, not both")
        saved: list[dict[str, Any]] | None = None
        saved_rows = None
        if report_id is not None:
            path = f"/Reports/{int(report_id)}/ReportParameters"
            kind = "report"
            ident = int(report_id)
            rows = srv.client().get_all(path, pagesize=100, max_pages=3)
        else:
            ident = int(memorized_report_id)
            path = f"/MemorizedReports/{ident}/RunParameters"
            kind = "memorized"
            rows = srv.client().get_all(path, pagesize=100, max_pages=3)
            saved_rows = srv.client().get_all(
                f"/MemorizedReports/{ident}/MemorizedReportSavedReportParameterValues",
                pagesize=100,
                max_pages=3,
            )
            saved = [normalize_saved_report_value(item) for item in saved_rows if isinstance(item, dict)]
        parameters = [normalize_report_parameter(item) for item in rows if isinstance(item, dict)]
        parameters.sort(key=lambda row: (row.get("sort_order") is None, row.get("sort_order") or 0, str(row.get("parameter") or "")))
        cap = 100
        row_capped = len(parameters) > cap
        parameters = parameters[:cap]
        source = srv._merged_source(rows, saved_rows) if saved_rows is not None else rows
        payload: dict[str, Any] = {
            "report_kind": kind,
            "id": ident,
            "parameters": parameters,
            "parameter_note": REPORT_PARAMETER_NOTE,
        }
        if saved is not None:
            payload["saved_values"] = saved[:cap]
            payload["saved_values_note"] = SAVED_VALUE_NOTE
        return srv._ok(
            payload,
            **srv._cutoff_meta(
                source,
                len(parameters),
                row_capped=row_capped,
                count=len(parameters),
                report_kind=kind,
                id=ident,
                endpoint=path,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _parse_parameters(raw: str | None) -> list[tuple[str, str]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("parameters_json must be JSON: an object of name to value, or a list of {parameter, value}") from exc
    pairs: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        items = list(payload.items())
    elif isinstance(payload, list):
        items = []
        for entry in payload:
            if not isinstance(entry, dict):
                raise ValueError("Each parameter entry must be an object with parameter and value")
            name = entry.get("parameter", entry.get("Parameter", entry.get("name")))
            if "value" in entry:
                value = entry.get("value")
            elif "Value" in entry:
                value = entry.get("Value")
            else:
                value = ""
            items.append((name, value))
    else:
        raise ValueError("parameters_json must be a JSON object or list")
    for name, value in items:
        token = str(name or "").strip()
        if not _PARAMETER_NAME.match(token):
            raise ValueError(
                f"Parameter name {token!r} is not a plain identifier. "
                "Use the parameter field from rm_report_parameters."
            )
        if value is None:
            text_value = ""
        else:
            text_value = str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text_value) > 500:
            raise ValueError(f"Parameter {token} is longer than 500 characters")
        pairs.append((token, text_value))
    if len(pairs) > 30:
        raise ValueError("At most 30 report parameters")
    return pairs


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None) or {}
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            raw = str(value)
            break
    return raw.split(";", 1)[0].strip().lower()


def _looks_like_download(value: str) -> bool:
    text = value.strip()
    if len(text) > 500:
        return False
    if text.startswith("/") or text.lower().startswith("http://") or text.lower().startswith("https://"):
        lowered = text.lower()
        return "downloadfile" in lowered or "downloadpdf" in lowered
    return False


def _download_reference(content: bytes, content_type: str) -> str | None:
    if not content:
        return None
    payload: Any = None
    if "json" in content_type or content[:1] in (b"{", b"[", b'"'):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    if isinstance(payload, str) and _looks_like_download(payload):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("Url", "URL", "DownloadUrl", "FileUrl", "ReportUrl"):
            value = payload.get(key)
            if isinstance(value, str) and _looks_like_download(value):
                return value.strip()
        report_key = payload.get("ReportKey") or payload.get("reportKey")
        if isinstance(report_key, str) and report_key.strip():
            return "/Reports/DownloadFile?reportKey=" + report_key.strip()
    try:
        text = content.decode("utf-8").strip().strip('"')
    except UnicodeDecodeError:
        return None
    if _looks_like_download(text):
        return text
    return None


def _download_target(url: str, base: str) -> tuple[str, dict[str, str]] | None:
    """Return a same-host DownloadFile path and query, or None."""
    text = url.strip()
    parsed_base = urlparse(base)
    if text.startswith("/"):
        parsed = urlparse(text)
    else:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() != parsed_base.netloc.lower():
            return None
    path = parsed.path or ""
    allowed = path.rstrip("/").lower() in {"/reports/downloadfile", "/reportbatches/downloadpdf"}
    if not allowed:
        return None
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    return path, query


def _cap_rows(rows: list[Any]) -> tuple[list[Any], bool]:
    shown: list[Any] = []
    truncated = False
    for row in rows:
        shown.append(row)
        if len(shown) > _REPORT_ROW_CAP:
            shown.pop()
            truncated = True
            break
        try:
            size = len(json.dumps(shown, default=str))
        except TypeError:
            size = _REPORT_TEXT_CAP + 1
        if size > _REPORT_TEXT_CAP:
            shown.pop()
            truncated = True
            break
    if len(rows) > len(shown):
        truncated = True
    return shown, truncated


def _file_summary(content: bytes, content_type: str) -> dict[str, Any]:
    label = content_type or "application/octet-stream"
    return {
        "output_kind": "file",
        "content_type": label,
        "byte_length": len(content),
        "summary": (
            f"Rent Manager returned a file ({label}, {len(content)} bytes). "
            "The file body is not included."
        ),
    }


def summarize_report_body(content: bytes, content_type: str) -> tuple[dict[str, Any], bool]:
    """Turn a RunReport or DownloadFile body into a bounded staff-facing payload."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not content:
        return {
            "output_kind": "empty",
            "content_type": ctype or None,
            "summary": "Rent Manager returned an empty body.",
            "byte_length": 0,
        }, False
    if content.startswith(b"%PDF") or "pdf" in ctype or "excel" in ctype or "spreadsheet" in ctype or ctype == "application/octet-stream":
        return _file_summary(content, ctype or "application/octet-stream"), False
    payload: Any = None
    if "json" in ctype or content[:1] in (b"{", b"[", b'"'):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    if payload is not None:
        if isinstance(payload, list):
            shown, truncated = _cap_rows(payload)
            return {
                "output_kind": "rows",
                "content_type": ctype or "application/json",
                "rows": shown,
                "row_count": len(shown),
                "byte_length": len(content),
            }, truncated
        if isinstance(payload, dict):
            rows = None
            for key in ("Rows", "Data", "Results", "Grid", "ReportData", "Items", "ReportRows", "Records"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
            if rows is not None:
                shown, truncated = _cap_rows(rows)
                header = {
                    key: payload.get(key)
                    for key in ("Name", "ReportName", "Title", "Description")
                    if key in payload and not isinstance(payload.get(key), (list, dict))
                }
                body = {
                    "output_kind": "rows",
                    "content_type": ctype or "application/json",
                    "rows": shown,
                    "row_count": len(shown),
                    "byte_length": len(content),
                }
                if header:
                    body["header"] = header
                return body, truncated
            raw = json.dumps(payload, default=str)
            truncated = len(raw) > _REPORT_TEXT_CAP
            if truncated:
                return {
                    "output_kind": "text",
                    "content_type": ctype or "application/json",
                    "text": raw[:_REPORT_TEXT_CAP],
                    "byte_length": len(content),
                }, True
            return {
                "output_kind": "object",
                "content_type": ctype or "application/json",
                "data": payload,
                "byte_length": len(content),
            }, False
        if isinstance(payload, str):
            truncated = len(payload) > _REPORT_TEXT_CAP
            return {
                "output_kind": "text",
                "content_type": ctype or "application/json",
                "text": payload[:_REPORT_TEXT_CAP],
                "byte_length": len(content),
            }, truncated
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _file_summary(content, ctype or "application/octet-stream"), False
    truncated = len(text) > _REPORT_TEXT_CAP
    return {
        "output_kind": "text",
        "content_type": ctype or "text/plain",
        "text": text[:_REPORT_TEXT_CAP],
        "byte_length": len(content),
    }, truncated


def run_report(
    report_id: int | None = None,
    memorized_report_id: int | None = None,
    parameters_json: str = "",
    output: str = "",
) -> str:
    srv = _deps()
    try:
        if report_id is None and memorized_report_id is None:
            raise ValueError("Pass report_id or memorized_report_id")
        if report_id is not None and memorized_report_id is not None:
            raise ValueError("Pass report_id or memorized_report_id, not both")
        pairs = _parse_parameters(parameters_json)
        requested = (output or "").strip().lower()
        get_option = None
        if requested not in {"", "default", "omit"}:
            get_option = _REPORT_GET_OPTIONS.get(requested)
            if get_option is None:
                known = ", ".join(["default", *_REPORT_GET_OPTIONS])
                raise ValueError(f"output must be one of: {known}")
        if report_id is not None:
            kind = "report"
            ident = int(report_id)
            path = f"/Reports/{ident}/RunReport"
        else:
            kind = "memorized"
            ident = int(memorized_report_id)
            path = f"/MemorizedReports/{ident}/RunReport"
        params: dict[str, Any] = {}
        for index, (name, value) in enumerate(pairs):
            params[f"parameters[{index}].Parameter"] = name
            params[f"parameters[{index}].Value"] = value
        if get_option:
            params["getOptions"] = get_option
        response = srv.client().request("GET", path, params=params or None, timeout=90)
        content = response.content or b""
        ctype = _content_type(response)
        followed = False
        download_url = _download_reference(content, ctype)
        if download_url:
            target = _download_target(download_url, srv.client().base)
            if target is None:
                body = {
                    "report_kind": kind,
                    "id": ident,
                    "output_kind": "url",
                    "content_type": ctype or None,
                    "summary": (
                        "Rent Manager returned a download link that is not on this Rent Manager "
                        "host, so it was not fetched."
                    ),
                    "byte_length": len(content),
                }
                return srv._ok(
                    body,
                    truncated=False,
                    total=None,
                    endpoint=path,
                    get_option=get_option,
                    followed_download=False,
                )
            download_path, download_params = target
            response = srv.client().request("GET", download_path, params=download_params or None, timeout=90)
            content = response.content or b""
            ctype = _content_type(response)
            followed = True
        body, truncated = summarize_report_body(content, ctype)
        body["report_kind"] = kind
        body["id"] = ident
        notice = None
        if truncated:
            if body.get("output_kind") == "rows":
                notice = (
                    f"CUT OFF: showing the first {body.get('row_count')} report row(s). "
                    "Narrow the report parameters for the rest."
                )
            else:
                notice = (
                    f"CUT OFF: report text was shortened to {_REPORT_TEXT_CAP} characters."
                )
        return srv._ok(
            body,
            truncated=truncated,
            total=None,
            notice=notice,
            endpoint=path,
            get_option=get_option,
            followed_download=followed,
            output=requested or "default",
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)
