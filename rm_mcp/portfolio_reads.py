"""Portfolio read tools: vacancy outlook, delinquency, and the renewal pipeline.

Every Rent Manager call from this module is a GET. Nested embeds that Rent
Manager rejects (RecurringCharges.ChargeType, LeaseRenewals.LeaseRenewalIncreaseOffer)
are not sent. Permission errors propagate. A 404 is "none" only on the
make-ready post list, the unit-status collection (which then falls back to
per-unit reads), and a single increase-offer subresource.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .client import PageList, RMError
from .normalize import normalize_increase_offer, normalize_open_receivable, normalize_unit
from . import ops_reads as ops

_ID_CHUNK = 50
_STATUS_FALLBACK_CAP = 40
_VACANT_EMBEDS = "IsVacant,CurrentOccupancyStatus,OccupancyStatusHistory,UserDefinedValues"
_STATUS_ORDER = ("no_renewal", "renewal_on_file", "signed")
_STAGE_ORDER = (
    "signed",
    "renewal_on_file",
    "offer_approved",
    "offer_set",
    "notice_given",
    "no_renewal",
)

VACANCY_NOTE = (
    "IsVacant is not a filterable field. Vacancy is applied here from the IsVacant embed. "
    "A unit whose IsVacant value is not a boolean is counted as unclassified and left out of both lists. "
    "days_vacant is today minus the latest OccupancyStatusHistory EndDate. "
    "A last EndDate of 2026-09-30 is 41 days vacant on 2026-11-10. "
    "There is no days_vacant when that history has no EndDate. "
    "coming_available is an occupied unit. The availability date is ExpectedMoveOutDate, "
    "otherwise the occupancy EndDate, otherwise NoticeDate. The unit is included when that "
    "date is on or before today plus within_days. A past date stays while the unit is still occupied. "
    "Make-ready status is the UnitStatuses row with no ToDate, or a ToDate on or after today. "
    "CurrentUnitStatus.UnitStatusType is not requested; that embed comes back empty. "
    "Asking rent and list_for_lease are unit UDFs 732 and 731. Dates are sorted here. No orderby is sent."
)
DELINQUENCY_SELECTION_NOTE = (
    "Candidates are selected with Rent Manager's Balance filter field "
    "(current tenants, balance greater than the minimum, and active properties unless a property id is passed). "
    "A tenant whose filter value disagrees with GET /Tenants/{id}/Balance can be missed, because they never enter this list. "
    "Among the tenants who were returned, embed_balance is the value on the tenant row and balance is the endpoint amount. "
    "Disagreements are listed in balance_mismatches. "
    "GET /Tenants/{id}/OpenBalance does not exist and is not called."
)
AGING_NOTE = (
    "Open receivable rows have no due date. Buckets use TransactionDate compared with today: "
    "0-30 (including today and a future date), 31-60, 61-90 (a charge 90 days old lands here), "
    "and 90+ (91 days and older). The amount in each bucket is AmountDue, which is already the open amount. "
    "A line with no TransactionDate is added to undated and is not dropped. "
    "The tenant balance is not the sum of these lines."
)
STAGE_NOTE = (
    "stage is the first match, most advanced first: "
    "signed (a later LeaseRenewal has SignDate); "
    "renewal_on_file (a later LeaseRenewal exists and has no SignDate); "
    "offer_approved (the increase offer on the next term, otherwise the current term, has ApprovalDate); "
    "offer_set (that offer has an amount or SetDate and no ApprovalDate); "
    "notice_given (the lease, or the unit CurrentOccupancyStatus, has NoticeDate or ExpectedMoveOutDate, and none of the above); "
    "no_renewal otherwise. "
    "notice_given is also its own boolean, so a signed renewal can still show that notice was given. "
    "LeaseRenewals.LeaseRenewalIncreaseOffer on /Leases returns 404 KeyNotFoundException and is not requested. "
    "Offers are GET /LeaseRenewals?filters=ParentLeaseID,in,(...)&embeds=LeaseRenewalIncreaseOffer, "
    "50 lease ids at a time. A 404 on that batch falls back to "
    "GET /LeaseRenewals/{id}/LeaseRenewalIncreaseOffer for the next term and then the current term. "
    "A 404 on one renewal means no offer. A 403 is returned as an error and is not turned into an empty offer."
)


def _deps():
    from . import server

    return server


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _ident(value: Any) -> int | None:
    return _deps()._id_of(value)


def _idents(values: list[Any]) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for value in values:
        ident = _ident(value)
        if ident is None or ident in seen:
            continue
        seen.add(ident)
        found.append(ident)
    return found


def _one_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _embed_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().casefold()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _money(value: float) -> float:
    return round(float(value), 2)


def _balance_gt(threshold: float) -> str:
    if threshold <= 0:
        return "Balance,gt,0"
    text = f"{threshold:.2f}".rstrip("0").rstrip(".")
    return f"Balance,gt,{text}"


def _property_filters(property_id: int | None) -> str:
    if property_id is not None:
        return f"PropertyID,eq,{int(property_id)}"
    return "IsActive,eq,true"


def _load_properties(property_id: int | None) -> Any:
    return _deps().client().get_all(
        "/Properties",
        filters=_property_filters(property_id),
        pagesize=200,
        max_pages=5,
    )


def _property_names(rows: Any) -> dict[int, Any]:
    names: dict[int, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ident = _ident(row.get("PropertyID"))
        if ident is None or ident in names:
            continue
        names[ident] = row.get("Name")
    return names


def _in_filter(field: str, ids: list[int]) -> str:
    return f"{field},in,({','.join(str(ident) for ident in ids)})"


def _history_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _vacancy_age(history: Any, today: date) -> tuple[int | None, str | None, Any]:
    """days_vacant, last move-out, and the tenant on that occupancy."""
    best_end: date | None = None
    best_start: date | None = None
    best_tenant: Any = None
    for row in _history_rows(history):
        end = ops._day(row.get("EndDate"))
        if end is None:
            continue
        start = ops._day(row.get("StartDate")) or date.min
        if best_end is None or end > best_end or (end == best_end and start >= (best_start or date.min)):
            best_end = end
            best_start = start
            best_tenant = row.get("TenantID")
    if best_end is None:
        return None, None, None
    return (today - best_end).days, best_end.isoformat(), best_tenant


def _availability(occupancy: dict[str, Any], today: date, window: int) -> tuple[date, str] | None:
    """The date a still-occupied unit comes available, when it falls inside the window."""
    occ_type = str(occupancy.get("OccupancyType") or "").strip()
    if occ_type and occ_type != "CurrentOccupant":
        return None
    expected = ops._day(occupancy.get("ExpectedMoveOutDate"))
    end = ops._day(occupancy.get("EndDate"))
    notice = ops._day(occupancy.get("NoticeDate"))
    if expected is not None:
        chosen, reason = expected, "expected_move_out"
    elif end is not None:
        chosen, reason = end, "occupancy_end"
    elif notice is not None:
        chosen, reason = notice, "notice"
    else:
        return None
    if (chosen - today).days > window:
        return None
    return chosen, reason


def _current_unit_status(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    current: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        to_day = ops._day(row.get("ToDate"))
        if row.get("ToDate") not in (None, "") and to_day is None:
            continue
        if to_day is None or to_day >= today:
            current.append(row)
    if not current:
        return {
            "make_ready_status": None,
            "make_ready_description": None,
            "make_ready_since": None,
            "make_ready_comment": None,
        }
    row = max(
        current,
        key=lambda item: (
            ops._day(item.get("FromDate")) or date.min,
            _ident(item.get("UnitStatusID")) or 0,
        ),
    )
    kind = row.get("UnitStatusType") if isinstance(row.get("UnitStatusType"), dict) else {}
    return {
        "make_ready_status": kind.get("Name"),
        "make_ready_description": kind.get("Description"),
        "make_ready_since": ops._date_iso(ops._day(row.get("FromDate"))),
        "make_ready_comment": row.get("Comment"),
    }


def _latest_make_ready_post(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [row for row in rows if isinstance(row, dict)]
    if not usable:
        return None
    open_rows = [row for row in usable if ops._day(row.get("CompletionDate")) is None and row.get("CompletionDate") in (None, "")]
    pool = open_rows or usable
    row = max(
        pool,
        key=lambda item: (
            ops._day(item.get("StartDate")) or date.min,
            _ident(item.get("MakeReadyPostID")) or 0,
        ),
    )
    completed = ops._day(row.get("CompletionDate"))
    return {
        "make_ready_post_id": row.get("MakeReadyPostID"),
        "start_date": ops._date_iso(ops._day(row.get("StartDate"))),
        "completion_date": ops._date_iso(completed),
        "comments": row.get("Comments"),
        "open": completed is None and row.get("CompletionDate") in (None, ""),
    }


def _load_unit_statuses(
    unit_ids: list[int],
) -> tuple[dict[int, list[dict[str, Any]]], list[Any], str, bool]:
    """Unit status history for the vacant set.

    The collection filter is the one that works. A 404 falls back to
    GET /Units/{id}/UnitStatuses for vacant units only, capped. Any other
    error, including 403, propagates.
    """
    grouped: dict[int, list[dict[str, Any]]] = {unit_id: [] for unit_id in unit_ids}
    if not unit_ids:
        return grouped, [], "skipped", False
    srv = _deps()
    views: list[Any] = []
    try:
        for chunk in _chunks(sorted(unit_ids), _ID_CHUNK):
            rows = srv.client().get_all(
                "/Units/UnitStatuses",
                filters=_in_filter("UnitID", chunk),
                embeds="UnitStatusType",
                pagesize=200,
                max_pages=10,
            )
            views.append(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ident = _ident(row.get("UnitID"))
                if ident in grouped:
                    grouped[ident].append(row)
        return grouped, views, "collection", False
    except RMError as exc:
        if exc.status != 404:
            raise
    grouped = {unit_id: [] for unit_id in unit_ids}
    views = []
    capped = len(unit_ids) > _STATUS_FALLBACK_CAP
    for unit_id in sorted(unit_ids)[:_STATUS_FALLBACK_CAP]:
        try:
            rows = srv.client().get_all(
                f"/Units/{unit_id}/UnitStatuses",
                embeds="UnitStatusType",
                pagesize=50,
                max_pages=4,
            )
        except RMError as exc:
            if exc.status == 404:
                rows = PageList([], truncated=False, total=0)
            else:
                raise
        views.append(rows)
        grouped[unit_id] = [row for row in rows if isinstance(row, dict)]
    return grouped, views, "per_unit", capped


def _load_make_ready_posts() -> tuple[Any, str | None]:
    """GET /MakeReadyPosts. 404 means the list is absent. 403 propagates."""
    try:
        rows = _deps().client().get_all("/MakeReadyPosts", pagesize=100, max_pages=5)
    except RMError as exc:
        if exc.status != 404:
            raise
        return PageList([], truncated=False, total=0), "GET /MakeReadyPosts returned 404 and was skipped."
    return rows, None


def _posts_by_unit(rows: Any) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ident = _ident(row.get("UnitID"))
        if ident is None:
            continue
        grouped.setdefault(ident, []).append(row)
    return grouped


def _listing_fields(unit: dict[str, Any]) -> dict[str, Any]:
    dto = normalize_unit(unit)
    return {
        "asking_rent": dto.get("asking_rent"),
        "list_for_lease": dto.get("list_for_lease"),
    }


def _property_vacancy_counts(
    vacant: list[dict[str, Any]],
    coming: list[dict[str, Any]],
    names: dict[int, Any],
) -> list[dict[str, Any]]:
    counts: dict[Any, dict[str, int]] = {}
    order: list[Any] = []

    def bump(property_id: Any, key: str) -> None:
        if property_id not in counts:
            counts[property_id] = {"vacant": 0, "coming_available": 0}
            order.append(property_id)
        counts[property_id][key] += 1

    for row in vacant:
        bump(row.get("property_id"), "vacant")
    for row in coming:
        bump(row.get("property_id"), "coming_available")
    summary = []
    for property_id in order:
        ident = _ident(property_id)
        summary.append(
            {
                "property_id": property_id,
                "property_name": names.get(ident) if ident is not None else None,
                "vacant": counts[property_id]["vacant"],
                "coming_available": counts[property_id]["coming_available"],
            }
        )
    summary.sort(key=lambda row: (str(row.get("property_name") or ""), str(row.get("property_id") or "")))
    return summary


def vacancy_outlook(
    property_id: int | None = None,
    within_days: int = 60,
    limit: int = 300,
) -> str:
    """Vacant units and occupied units coming available. See the server tool docstring."""
    srv = _deps()
    try:
        window = srv._clamp(60 if within_days is None else int(within_days), 0, 366)
        cap = srv._clamp(limit, 1, 1000)
        today = ops._today()
        filters = "Property.IsActive,eq,true"
        if property_id is not None:
            filters = f"{filters};PropertyID,eq,{int(property_id)}"
        units = srv.client().get_all(
            "/Units",
            filters=filters,
            embeds=_VACANT_EMBEDS,
            pagesize=200,
            max_pages=10,
        )
        properties = _load_properties(property_id)
        names = _property_names(properties)
        vacant_units: list[dict[str, Any]] = []
        coming_units: list[tuple[dict[str, Any], dict[str, Any], date, str]] = []
        unclassified = 0
        for raw in units:
            if not isinstance(raw, dict):
                continue
            vacant_flag = _embed_bool(raw.get("IsVacant"))
            if vacant_flag is None:
                unclassified += 1
                continue
            if vacant_flag:
                vacant_units.append(raw)
                continue
            occupancy = _one_dict(raw.get("CurrentOccupancyStatus"))
            available = _availability(occupancy, today, window)
            if available is None:
                continue
            coming_units.append((raw, occupancy, available[0], available[1]))

        status_ids = _idents([unit.get("UnitID") for unit in vacant_units])
        statuses, status_views, status_mode, status_capped = _load_unit_statuses(status_ids)
        post_rows: Any = PageList([], truncated=False, total=0)
        post_note = None
        if vacant_units:
            post_rows, post_note = _load_make_ready_posts()
        posts = _posts_by_unit(post_rows)

        vacant: list[dict[str, Any]] = []
        for raw in vacant_units:
            ident = _ident(raw.get("UnitID"))
            property_ident = _ident(raw.get("PropertyID"))
            days_vacant, last_move_out, last_tenant_id = _vacancy_age(raw.get("OccupancyStatusHistory"), today)
            status = _current_unit_status(statuses.get(ident, []) if ident is not None else [], today)
            vacant.append(
                {
                    "unit_id": raw.get("UnitID") if ident is None else ident,
                    "unit_name": raw.get("Name"),
                    "property_id": raw.get("PropertyID") if property_ident is None else property_ident,
                    "property_name": names.get(property_ident) if property_ident is not None else None,
                    "days_vacant": days_vacant,
                    "last_move_out": last_move_out,
                    "last_tenant_id": last_tenant_id,
                    **status,
                    "make_ready_post": _latest_make_ready_post(posts.get(ident, [])) if ident is not None else None,
                    **_listing_fields(raw),
                }
            )
        vacant.sort(
            key=lambda row: (
                row.get("days_vacant") is None,
                -(row.get("days_vacant") or 0),
                str(row.get("property_name") or ""),
                str(row.get("unit_name") or ""),
            )
        )

        tenant_ids = _idents(
            [_one_dict(raw.get("CurrentOccupancyStatus")).get("TenantID") for raw, _occ, _day, _why in coming_units]
        )
        tenant_views: list[Any] = []
        tenant_names: dict[int, Any] = {}
        for chunk in _chunks(sorted(tenant_ids), _ID_CHUNK):
            rows = srv.client().get_all(
                "/Tenants",
                filters=_in_filter("TenantID", chunk),
                pagesize=100,
                max_pages=5,
            )
            tenant_views.append(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ident = _ident(row.get("TenantID"))
                if ident is not None and ident not in tenant_names:
                    tenant_names[ident] = row.get("Name")

        coming: list[dict[str, Any]] = []
        for raw, occupancy, available_on, reason in coming_units:
            ident = _ident(raw.get("UnitID"))
            property_ident = _ident(raw.get("PropertyID"))
            tenant_ident = _ident(occupancy.get("TenantID"))
            coming.append(
                {
                    "unit_id": raw.get("UnitID") if ident is None else ident,
                    "unit_name": raw.get("Name"),
                    "property_id": raw.get("PropertyID") if property_ident is None else property_ident,
                    "property_name": names.get(property_ident) if property_ident is not None else None,
                    "tenant_id": occupancy.get("TenantID") if tenant_ident is None else tenant_ident,
                    "tenant_name": tenant_names.get(tenant_ident) if tenant_ident is not None else None,
                    "notice_date": ops._date_iso(ops._day(occupancy.get("NoticeDate"))),
                    "expected_move_out": ops._date_iso(ops._day(occupancy.get("ExpectedMoveOutDate"))),
                    "occupancy_end": ops._date_iso(ops._day(occupancy.get("EndDate"))),
                    "available_on": available_on.isoformat(),
                    "days_until_available": (available_on - today).days,
                    "availability_from": reason,
                    **_listing_fields(raw),
                }
            )
        coming.sort(
            key=lambda row: (
                row.get("days_until_available") is None,
                row.get("days_until_available") if row.get("days_until_available") is not None else 0,
                str(row.get("property_name") or ""),
                str(row.get("unit_name") or ""),
            )
        )
        by_property = _property_vacancy_counts(vacant, coming, names)
        shown_vacant = vacant[:cap]
        shown_coming = coming[:cap]
        row_capped = len(vacant) > cap or len(coming) > cap
        source = srv._merged_source(units, properties, *status_views, post_rows, *tenant_views)
        notice = None
        if source.truncated or row_capped or status_capped:
            parts = [
                "CUT OFF: this is not the full vacancy outlook.",
                "A unit, property, status, make-ready, or tenant-name download stopped early, or a row limit was hit.",
                "by_property counts the units downloaded before the display limit.",
                "Do not treat the lists as every vacant or coming-available unit.",
            ]
            if status_capped:
                parts.append(
                    f"Make-ready status was read for the first {_STATUS_FALLBACK_CAP} vacant units only."
                )
            notice = " ".join(parts)
        if post_note and notice:
            notice = f"{notice} {post_note}"
        return srv._ok(
            {
                "vacant": shown_vacant,
                "coming_available": shown_coming,
                "by_property": by_property,
            },
            **srv._cutoff_meta(
                source,
                len(shown_vacant) + len(shown_coming),
                row_capped=row_capped or status_capped,
                notice=notice,
                count=len(shown_vacant) + len(shown_coming),
                property_id=property_id,
                within_days=window,
                vacant_count=len(vacant),
                coming_available_count=len(coming),
                shown_vacant=len(shown_vacant),
                shown_coming_available=len(shown_coming),
                unclassified=unclassified,
                unit_status_mode=status_mode,
                unit_status_capped=status_capped,
                vacancy_note=VACANCY_NOTE,
                **({"make_ready_note": post_note} if post_note else {}),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _delinquency_filters(
    property_id: int | None,
    min_balance: float | None,
    include_past: bool,
) -> tuple[str, float]:
    threshold = 0.0 if min_balance is None else float(min_balance)
    if threshold < 0:
        raise ValueError("min_balance cannot be negative")
    parts = []
    if not include_past:
        parts.append("Status,eq,Current")
    parts.append(_balance_gt(threshold))
    if property_id is not None:
        parts.append(f"PropertyID,eq,{int(property_id)}")
    else:
        parts.append("Property.IsActive,eq,true")
    return ";".join(parts), threshold


def _delinquency_embeds(include_aging: bool, include_rent: bool) -> str:
    parts = ["Balance"]
    if include_aging:
        parts.append("OpenReceivables")
    parts.append("Leases")
    if include_rent:
        parts.append("RecurringCharges")
    return ",".join(parts)


def _embed_balance(tenant: dict[str, Any]) -> float | None:
    amount, _field = _deps()._read_balance(tenant.get("Balance"))
    return amount


def _tenant_name(tenant: dict[str, Any]) -> str | None:
    name = tenant.get("Name")
    if name is not None and str(name).strip():
        return str(name)
    parts = [str(tenant.get("FirstName") or "").strip(), str(tenant.get("LastName") or "").strip()]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _aging(raw_rows: Any, today: date) -> tuple[dict[str, float], str | None]:
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0, "undated": 0.0}
    oldest: date | None = None
    for raw in ops._rows(raw_rows):
        dto = normalize_open_receivable(raw)
        due = dto.get("amount_due")
        amount = 0.0 if due is None else float(due)
        charged_on = ops._day(dto.get("transaction_date"))
        if charged_on is None:
            buckets["undated"] += amount
            continue
        age = (today - charged_on).days
        if age <= 30:
            key = "0-30"
        elif age <= 60:
            key = "31-60"
        elif age <= 90:
            key = "61-90"
        else:
            key = "90+"
        buckets[key] += amount
        if oldest is None or charged_on < oldest:
            oldest = charged_on
    return {key: _money(value) for key, value in buckets.items()}, ops._date_iso(oldest)


def _unit_refs(tenant: dict[str, Any], receivables: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()

    def add(unit_id: Any, unit_name: Any) -> None:
        ident = _ident(unit_id)
        key = (ident if ident is not None else unit_id, unit_name)
        if key in seen or (ident is None and unit_id is None and not unit_name):
            return
        seen.add(key)
        refs.append({"unit_id": ident if ident is not None else unit_id, "unit_name": unit_name})

    for lease in tenant.get("Leases") or []:
        if not isinstance(lease, dict):
            continue
        if lease.get("MoveOutDate") or lease.get("DepartureDate"):
            continue
        unit = lease.get("Unit") if isinstance(lease.get("Unit"), dict) else {}
        add(lease.get("UnitID") if lease.get("UnitID") is not None else unit.get("UnitID"), unit.get("Name"))
    for raw in ops._rows(receivables):
        dto = normalize_open_receivable(raw)
        add(dto.get("unit_id"), dto.get("unit_name"))
    return refs


def _units_by_id(rows: Any) -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ident = _ident(row.get("UnitID"))
        if ident is not None and ident not in found:
            found[ident] = row
    return found


def _tenant_rent(
    tenant: dict[str, Any],
    units_by_id: dict[int, dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    srv = _deps()
    charges: list[dict[str, Any]] = []
    for unit_id in srv._active_unit_ids(tenant):
        unit = units_by_id.get(unit_id)
        if unit is None:
            continue
        charges.extend(srv._charge_rows(unit.get("RecurringCharges")))
    return ops._rent_for(tenant, {"RecurringCharges": charges}, today)


def delinquency_summary(
    property_id: int | None = None,
    min_balance: float | None = None,
    include_past: bool = False,
    include_aging: bool = True,
    include_rent: bool = False,
    limit: int = 200,
) -> str:
    """Who is behind, grouped by property. See the server tool docstring."""
    srv = _deps()
    try:
        filters, threshold = _delinquency_filters(property_id, min_balance, include_past)
        cap = srv._clamp(limit, 1, 500)
        today = ops._today()
        tenants = srv.client().get_all(
            "/Tenants",
            filters=filters,
            embeds=_delinquency_embeds(include_aging, include_rent),
            pagesize=100,
            max_pages=20,
        )
        properties = _load_properties(property_id)
        names = _property_names(properties)
        unit_rows: Any = None
        charge_types: Any = None
        units_by_id: dict[int, dict[str, Any]] = {}
        if include_rent:
            unit_filters = (
                f"PropertyID,eq,{int(property_id)}" if property_id is not None else "Property.IsActive,eq,true"
            )
            unit_rows = srv.client().get_all(
                "/Units",
                filters=unit_filters,
                embeds=srv._UNIT_RENT_EMBEDS,
                pagesize=200,
                max_pages=10,
            )
            charge_types = srv._load_charge_type_catalog(tenants, unit_rows)
            units_by_id = _units_by_id(unit_rows)

        candidates = [row for row in tenants if isinstance(row, dict)]
        candidates.sort(
            key=lambda row: (
                -(_embed_balance(row) or 0.0),
                _ident(row.get("TenantID")) or 0,
            )
        )
        balance_capped = len(candidates) > cap
        chosen = candidates[:cap]
        mismatches: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        unconfirmed: list[dict[str, Any]] = []
        missing_receivables = 0
        balance_requests = 0
        built: list[dict[str, Any]] = []
        for tenant in chosen:
            tenant_id = _ident(tenant.get("TenantID"))
            if tenant_id is None:
                continue
            embed_amount = _embed_balance(tenant)
            balance, balance_field = srv._read_balance_response(f"/Tenants/{tenant_id}/Balance")
            balance_requests += 1
            if embed_amount is not None and balance is not None and _money(embed_amount) != _money(balance):
                mismatches.append(
                    {
                        "tenant_id": tenant_id,
                        "embed_balance": _money(embed_amount),
                        "endpoint_balance": _money(balance),
                    }
                )
            if balance is None:
                unconfirmed.append(
                    {
                        "tenant_id": tenant_id,
                        "name": _tenant_name(tenant),
                        "embed_balance": None if embed_amount is None else _money(embed_amount),
                        "reason": "GET /Tenants/{id}/Balance returned 404, so this balance was not confirmed.",
                    }
                )
            elif balance <= threshold:
                excluded.append(
                    {
                        "tenant_id": tenant_id,
                        "name": _tenant_name(tenant),
                        "embed_balance": None if embed_amount is None else _money(embed_amount),
                        "balance": _money(balance),
                        "reason": "GET /Tenants/{id}/Balance is not above the minimum, so this tenant is not counted as delinquent.",
                    }
                )
                continue
            receivables = tenant.get("OpenReceivables") if include_aging else None
            aging = None
            oldest = None
            if include_aging:
                if "OpenReceivables" not in tenant:
                    missing_receivables += 1
                else:
                    aging, oldest = _aging(receivables, today)
            property_ident = _ident(tenant.get("PropertyID"))
            row: dict[str, Any] = {
                "tenant_id": tenant_id,
                "name": _tenant_name(tenant),
                "status": tenant.get("Status"),
                "property_id": tenant.get("PropertyID") if property_ident is None else property_ident,
                "property_name": names.get(property_ident) if property_ident is not None else None,
                "units": _unit_refs(tenant, receivables),
                "balance": None if balance is None else _money(balance),
                "embed_balance": None if embed_amount is None else _money(embed_amount),
                "balance_field": balance_field,
                "balance_source": f"GET /Tenants/{tenant_id}/Balance",
                "balance_confirmed": balance is not None,
                "oldest_open_charge_date": oldest,
                "aging": aging,
            }
            if include_rent:
                rent = _tenant_rent(tenant, units_by_id, today)
                row["current_rent"] = rent.get("current_rent")
                row["base_rent_tenant"] = rent.get("base_rent_tenant")
                row["base_rent_subsidized"] = rent.get("base_rent_subsidized")
                if rent.get("rent_complete") is False:
                    row["rent_complete"] = False
                    row["unresolved_charges"] = rent.get("unresolved_charges")
            built.append(row)

        grouped: dict[Any, list[dict[str, Any]]] = {}
        group_order: list[Any] = []
        for row in built:
            key = row.get("property_id")
            if key not in grouped:
                grouped[key] = []
                group_order.append(key)
            grouped[key].append(row)
        properties_out = []
        for key in group_order:
            tenants_here = grouped[key]
            tenants_here.sort(
                key=lambda row: (
                    -(row["balance"] if row.get("balance") is not None else -10**12),
                    str(row.get("name") or ""),
                    row.get("tenant_id") or 0,
                )
            )
            total = _money(sum(row["balance"] for row in tenants_here if row.get("balance") is not None))
            properties_out.append(
                {
                    "property_id": key,
                    "property_name": tenants_here[0].get("property_name") if tenants_here else None,
                    "tenant_count": len(tenants_here),
                    "total_balance": total,
                    "tenants": tenants_here,
                }
            )
        properties_out.sort(
            key=lambda row: (
                -row["total_balance"],
                str(row.get("property_name") or ""),
                str(row.get("property_id") or ""),
            )
        )
        total_balance = _money(sum(row["total_balance"] for row in properties_out))
        confirmed = sum(1 for row in built if row.get("balance_confirmed"))
        source = srv._merged_source(tenants, properties, unit_rows, charge_types)
        notice = None
        if source.truncated or balance_capped:
            notice = (
                "CUT OFF: this is not every delinquent tenant. The tenant, property, unit, or "
                "charge-type download stopped early, or the row limit was hit before every "
                "candidate could be confirmed with GET /Tenants/{id}/Balance. "
                "Totals cover the tenants included here. The row limit keeps the highest "
                "Balance filter values first, then the endpoint can reorder those rows."
            )
        receivables_note = None
        if missing_receivables:
            receivables_note = (
                f"{missing_receivables} tenant(s) had no OpenReceivables embed, so their aging was left empty."
            )
            if notice:
                notice = f"{notice} {receivables_note}"
        balance_note = None
        if unconfirmed:
            balance_note = (
                f"{len(unconfirmed)} tenant(s) had no amount from GET /Tenants/{{id}}/Balance (404). "
                "They are listed with a null balance and are not included in the totals."
            )
        rent_meta: dict[str, Any] = {}
        if include_rent:
            rent_meta = {"rent_note": ops.RENT_NOTE, **ops._rent_gap(built)}
        return srv._ok(
            {
                "properties": properties_out,
                "totals": {
                    "tenant_count": len(built),
                    "property_count": len(properties_out),
                    "total_balance": total_balance,
                    "confirmed_balances": confirmed,
                },
            },
            **srv._cutoff_meta(
                source,
                len(built),
                row_capped=balance_capped,
                notice=notice,
                count=len(built),
                property_id=property_id,
                min_balance=threshold,
                include_past=bool(include_past),
                include_aging=bool(include_aging),
                include_rent=bool(include_rent),
                candidate_count=len(candidates),
                balance_requests=balance_requests,
                balance_mismatches=mismatches,
                excluded_after_balance_check=excluded,
                balance_unconfirmed=unconfirmed,
                **({"balance_note": balance_note} if balance_note else {}),
                **({"receivables_note": receivables_note} if receivables_note else {}),
                selection_note=DELINQUENCY_SELECTION_NOTE,
                aging_note=AGING_NOTE if include_aging else None,
                **rent_meta,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)


def _offer_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), None)
    if not isinstance(value, dict):
        return None
    return normalize_increase_offer(value)


def _pick_offer(
    raws: list[dict[str, Any]],
    next_id: Any,
    current_id: Any,
) -> dict[str, Any]:
    by_id: dict[int, dict[str, Any]] = {}
    for raw in raws:
        ident = _ident(raw.get("LeaseRenewalID"))
        if ident is not None and ident not in by_id:
            by_id[ident] = raw
    ordered: list[int] = []
    for value in (next_id, current_id):
        ident = _ident(value)
        if ident is not None and ident not in ordered:
            ordered.append(ident)
    rest = [ident for ident in by_id if ident not in ordered]
    rest.sort(key=lambda ident: (ops._day(by_id[ident].get("StartDate")) or date.min, ident))
    for ident in ordered + rest:
        raw = by_id.get(ident)
        if raw is None:
            continue
        offer = _offer_payload(raw.get("LeaseRenewalIncreaseOffer"))
        if offer:
            return {"increase_offer": offer, "offer_on_renewal_id": ident}
    return {"increase_offer": None, "offer_on_renewal_id": None}


def _empty_offer() -> dict[str, Any]:
    return {"increase_offer": None, "offer_on_renewal_id": None}


def _load_offers(rows: list[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], list[Any], str, int]:
    """Batched offer read, with a per-renewal fallback when the batch 404s.

    The nested /Leases embed is not tried. It 404s with KeyNotFoundException.
    A 403 on either path propagates.
    """
    lease_ids = sorted(
        {
            ident
            for ident in (_ident(row.get("lease_id")) for row in rows)
            if ident is not None
        }
    )
    if not lease_ids:
        return {}, [], "skipped", 0
    srv = _deps()
    views: list[Any] = []
    try:
        for chunk in _chunks(lease_ids, _ID_CHUNK):
            fetched = srv.client().get_all(
                "/LeaseRenewals",
                filters=_in_filter("ParentLeaseID", chunk),
                embeds="LeaseRenewalIncreaseOffer",
                pagesize=200,
                max_pages=10,
            )
            views.append(fetched)
    except RMError as exc:
        if exc.status != 404:
            raise
        return _load_offers_per_renewal(rows)
    by_lease: dict[int, list[dict[str, Any]]] = {}
    for view in views:
        for raw in view:
            if not isinstance(raw, dict):
                continue
            parent = _ident(raw.get("ParentLeaseID"))
            if parent is None:
                continue
            by_lease.setdefault(parent, []).append(raw)
    picked: dict[int, dict[str, Any]] = {}
    for row in rows:
        lease_id = _ident(row.get("lease_id"))
        if lease_id is None:
            continue
        picked[lease_id] = _pick_offer(
            by_lease.get(lease_id, []),
            row.get("next_renewal_id"),
            row.get("current_renewal_id"),
        )
    return picked, views, "batch", len(views)


def _load_offers_per_renewal(
    rows: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[Any], str, int]:
    """One GET per next, then current, renewal. 404 from _read_one means no offer."""
    picked: dict[int, dict[str, Any]] = {}
    calls = 0
    for row in rows:
        lease_id = _ident(row.get("lease_id"))
        if lease_id is None:
            continue
        found = _empty_offer()
        for value in (row.get("next_renewal_id"), row.get("current_renewal_id")):
            ident = _ident(value)
            if ident is None:
                continue
            calls += 1
            offer = _offer_payload(ops._read_one(f"/LeaseRenewals/{ident}/LeaseRenewalIncreaseOffer"))
            if offer:
                found = {"increase_offer": offer, "offer_on_renewal_id": ident}
                break
        picked[lease_id] = found
    return picked, [], "per_renewal", calls


def _stage(renewal_status: Any, offer: dict[str, Any] | None, notice_given: bool) -> str:
    if renewal_status == "signed":
        return "signed"
    if renewal_status == "renewal_on_file":
        return "renewal_on_file"
    offer_status = None if not offer else offer.get("offer_status")
    if offer_status == "approved":
        return "offer_approved"
    if offer_status == "set":
        return "offer_set"
    if notice_given:
        return "notice_given"
    return "no_renewal"


def _tally(rows: list[dict[str, Any]], key: str, order: tuple[str, ...]) -> dict[str, int]:
    counts = {name: 0 for name in order}
    for row in rows:
        name = str(row.get(key) or "")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _occupancy_by_unit(property_id: int | None) -> tuple[dict[int, dict[str, Any]], Any]:
    filters = (
        f"PropertyID,eq,{int(property_id)}" if property_id is not None else "Property.IsActive,eq,true"
    )
    rows = _deps().client().get_all(
        "/Units",
        filters=filters,
        embeds="CurrentOccupancyStatus",
        pagesize=200,
        max_pages=10,
    )
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = _ident(row.get("UnitID"))
        if ident is None or ident in found:
            continue
        found[ident] = _one_dict(row.get("CurrentOccupancyStatus"))
    return found, rows


def renewal_pipeline(
    property_id: int | None = None,
    within_days: int = 90,
    limit: int = 200,
) -> str:
    """Grouped renewal pipeline on top of the expiring-lease read. See the server tool docstring."""
    srv = _deps()
    try:
        collected = ops._collect_expiring_leases(property_id, within_days, limit)
        data: list[dict[str, Any]] = collected["data"]
        extra_views: list[Any] = []
        occupancy: dict[int, dict[str, Any]] = {}
        if data:
            occupancy, occupancy_rows = _occupancy_by_unit(property_id)
            extra_views.append(occupancy_rows)
            properties = _load_properties(property_id)
            extra_views.append(properties)
            names = _property_names(properties)
        else:
            names = {}
        for row in data:
            unit_id = _ident(row.get("unit_id"))
            occ = occupancy.get(unit_id, {}) if unit_id is not None else {}
            occ_notice = ops._date_iso(ops._day(occ.get("NoticeDate")))
            occ_expected = ops._date_iso(ops._day(occ.get("ExpectedMoveOutDate")))
            row["occupancy_notice_date"] = occ_notice
            row["occupancy_expected_move_out"] = occ_expected
            row["notice_given"] = bool(
                row.get("notice_date") or row.get("expected_move_out") or occ_notice or occ_expected
            )
            property_ident = _ident(row.get("property_id"))
            if not row.get("property_name") and property_ident is not None:
                row["property_name"] = names.get(property_ident)
        offers, offer_views, offer_mode, offer_requests = _load_offers(data)
        extra_views.extend(offer_views)
        for row in data:
            lease_id = _ident(row.get("lease_id"))
            picked = offers.get(lease_id, _empty_offer()) if lease_id is not None else _empty_offer()
            row["increase_offer"] = picked["increase_offer"]
            row["offer_on_renewal_id"] = picked["offer_on_renewal_id"]
            row["stage"] = _stage(row.get("renewal_status"), picked["increase_offer"], bool(row.get("notice_given")))
        by_status = _tally(data, "renewal_status", _STATUS_ORDER)
        by_stage = _tally(data, "stage", _STAGE_ORDER)
        by_property_rows: dict[Any, list[dict[str, Any]]] = {}
        property_order: list[Any] = []
        for row in data:
            key = row.get("property_id")
            if key not in by_property_rows:
                by_property_rows[key] = []
                property_order.append(key)
            by_property_rows[key].append(row)
        by_property = []
        for key in property_order:
            group = by_property_rows[key]
            by_property.append(
                {
                    "property_id": key,
                    "property_name": group[0].get("property_name") if group else None,
                    "count": len(group),
                    "by_status": _tally(group, "renewal_status", _STATUS_ORDER),
                    "by_stage": _tally(group, "stage", _STAGE_ORDER),
                }
            )
        by_property.sort(key=lambda row: (str(row.get("property_name") or ""), str(row.get("property_id") or "")))
        source = srv._merged_source(collected["lease_rows"], *collected["charge_views"], *extra_views)
        notice = None
        if source.truncated or collected["row_capped"]:
            notice = (
                "CUT OFF: this is not every expiring lease in the window. The lease, charge, "
                "unit, property, or offer download stopped early, or the row limit was hit. "
                "Counts describe the leases included here, not the ones left out. "
                "open_ended can be short too."
            )
        return srv._ok(
            {
                "leases": data,
                "counts": {
                    "leases": len(data),
                    "notice_given": sum(1 for row in data if row.get("notice_given")),
                    "with_offer": sum(1 for row in data if row.get("increase_offer")),
                    "by_status": by_status,
                    "by_stage": by_stage,
                },
                "by_property": by_property,
            },
            **srv._cutoff_meta(
                source,
                len(data),
                row_capped=collected["row_capped"],
                notice=notice,
                count=len(data),
                property_id=property_id,
                within_days=collected["window"],
                open_ended=collected["open_ended"],
                charge_mode=collected["charge_mode"],
                offer_mode=offer_mode,
                offer_requests=offer_requests,
                renewal_note=ops.RENEWAL_NOTE,
                rent_note=ops.RENT_NOTE,
                stage_note=STAGE_NOTE,
                **ops._rent_gap(data),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return srv._err(exc)
