"""Normalize raw Rent Manager payloads into stable DTOs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .client import UDF_ASKING_RENT, UDF_LIST_FOR_LEASE, UDF_LIST_ON_TENANT_TURNER


def _as_bool_yes_no(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"yes", "y", "true", "1"}:
        return True
    if s in {"no", "n", "false", "0"}:
        return False
    return None


def _as_decimal(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value).replace(",", "").replace("$", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def _as_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    return s[:10]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def udf_map(raw_udfs: Any) -> dict[str, Any]:
    """Map UDF list to both id-keyed and name-keyed dicts plus known fields."""
    by_id: dict[int, Any] = {}
    by_name: dict[str, Any] = {}
    if not isinstance(raw_udfs, list):
        raw_udfs = []
    for item in raw_udfs:
        if not isinstance(item, dict):
            continue
        fid = item.get("UserDefinedFieldID") or item.get("FieldID")
        name = item.get("Name") or item.get("FieldName")
        val = item.get("Value")
        if fid is not None:
            try:
                by_id[int(fid)] = val
            except (TypeError, ValueError):
                pass
        if name:
            by_name[str(name)] = val
    return {
        "by_id": by_id,
        "by_name": by_name,
        "list_for_lease": _as_bool_yes_no(by_id.get(UDF_LIST_FOR_LEASE)),
        "asking_rent": _as_decimal(by_id.get(UDF_ASKING_RENT)),
        "list_on_tenant_turner": _as_bool_yes_no(by_id.get(UDF_LIST_ON_TENANT_TURNER)),
    }


def normalize_property(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "property_id": raw.get("PropertyID") or raw.get("property_id"),
        "name": raw.get("Name") or raw.get("name"),
        "short_name": raw.get("ShortName") or raw.get("short_name"),
        "is_active": raw.get("IsActive", raw.get("is_active")),
    }


def normalize_address(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    street = raw.get("Street") or raw.get("Street1") or raw.get("Address")
    street2 = raw.get("Street2")
    city = raw.get("City")
    state = raw.get("State")
    postal = raw.get("PostalCode") or raw.get("Zip") or raw.get("ZipCode")
    if isinstance(street, str) and "," in street and not city:
        full = street
    else:
        if city and state and postal and street:
            full = f"{street}" + (f", {street2}" if street2 else "") + f", {city}, {state} {postal}"
        else:
            parts = [p for p in [street, street2, city, state, postal] if p]
            full = ", ".join(str(p) for p in parts) if parts else None
    return {
        "street": street,
        "street2": street2,
        "city": city,
        "state": state,
        "postal_code": postal,
        "full": full or raw.get("Address"),
    }


def normalize_unit(raw: dict[str, Any], address: dict[str, Any] | None = None) -> dict[str, Any]:
    udfs = udf_map(raw.get("UserDefinedValues") or raw.get("UserDefinedFieldValues") or [])
    return {
        "unit_id": raw.get("UnitID") or raw.get("unit_id"),
        "name": raw.get("Name") or raw.get("name"),
        "property_id": raw.get("PropertyID") or raw.get("property_id"),
        "bedrooms": raw.get("Bedrooms"),
        "bathrooms": raw.get("Bathrooms"),
        "sqft": raw.get("SquareFootage") or raw.get("sqft"),
        "list_for_lease": udfs["list_for_lease"],
        "asking_rent": udfs["asking_rent"],
        "list_on_tenant_turner": udfs["list_on_tenant_turner"],
        "udfs": {
            "list_for_lease_field_id": UDF_LIST_FOR_LEASE,
            "asking_rent_field_id": UDF_ASKING_RENT,
            "list_on_tenant_turner_field_id": UDF_LIST_ON_TENANT_TURNER,
            "selected": {
                "731": udfs["by_id"].get(UDF_LIST_FOR_LEASE),
                "732": udfs["by_id"].get(UDF_ASKING_RENT),
                "710": udfs["by_id"].get(UDF_LIST_ON_TENANT_TURNER),
            },
            "by_id": {str(k): v for k, v in udfs["by_id"].items()},
            "by_name": udfs["by_name"],
        },
        "address": address
        or normalize_address(raw.get("Address") if isinstance(raw.get("Address"), dict) else None),
    }


def normalize_contact(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": raw.get("ContactID"),
        "first_name": raw.get("FirstName"),
        "last_name": raw.get("LastName"),
        "email": raw.get("Email") or None,
        "phone": raw.get("Phone") or raw.get("MobilePhone") or raw.get("HomePhone"),
        "is_active": raw.get("IsActive"),
        "applicant_type": raw.get("ApplicantType"),
        "is_show_on_bill": raw.get("IsShowOnBill"),
        "parent_id": raw.get("ParentID"),
        "parent_type": raw.get("ParentType"),
    }


def normalize_tenant(raw: dict[str, Any]) -> dict[str, Any]:
    contacts_raw = raw.get("Contacts") or []
    contacts = [normalize_contact(c) for c in contacts_raw if isinstance(c, dict)]
    primary = None
    for c in contacts:
        if c.get("email") or c.get("phone"):
            primary = c
            break
    if primary is None and contacts:
        primary = contacts[0]

    phone = raw.get("Phone") or raw.get("MobilePhone")
    email = raw.get("Email")
    if primary:
        phone = phone or primary.get("phone")
        email = email or primary.get("email")

    return {
        "tenant_id": raw.get("TenantID") or raw.get("tenant_id"),
        "name": raw.get("Name") or raw.get("name"),
        "first_name": raw.get("FirstName"),
        "last_name": raw.get("LastName"),
        "status": raw.get("Status"),
        "property_id": raw.get("PropertyID"),
        "unit_id": raw.get("UnitID"),
        "move_in_date": _as_date(raw.get("MoveInDate")),
        "phone": phone,
        "email": email,
        "contacts": contacts,
    }


def normalize_lease(raw: dict[str, Any]) -> dict[str, Any]:
    departure = raw.get("DepartureDate") or raw.get("MoveOutDate")
    return {
        "lease_id": raw.get("LeaseID"),
        "tenant_id": raw.get("TenantID"),
        "unit_id": raw.get("UnitID"),
        "property_id": raw.get("PropertyID"),
        "move_in_date": _as_date(raw.get("MoveInDate") or raw.get("ArrivalDate")),
        "departure_date": _as_date(departure),
        "move_out_date": _as_date(raw.get("MoveOutDate")),
        "is_active": not bool(raw.get("DepartureDate")) and not bool(raw.get("MoveOutDate")),
    }


def normalize_udf_field(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_id": raw.get("UserDefinedFieldID"),
        "name": raw.get("Name"),
        "parent_type": raw.get("ParentType"),
        "field_type": raw.get("FieldType"),
        "default_value": raw.get("DefaultValue"),
        "combo_list": raw.get("ComboList"),
        "sort_order": raw.get("SortOrder"),
    }


def normalize_vendor(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_id": raw.get("VendorID"),
        "name": raw.get("Name"),
        "payee": raw.get("Payee"),
    }


def normalize_prospect(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "prospect_id": raw.get("ProspectID"),
        "display_id": raw.get("ProspectDisplayID"),
        "property_id": raw.get("PropertyID"),
        "name": raw.get("Name"),
        "first_name": raw.get("FirstName"),
        "last_name": raw.get("LastName"),
        "status": raw.get("ProspectStatus") or raw.get("Status"),
        "lead_source_id": raw.get("LeadSourceID"),
        "leasing_agent_id": raw.get("LeasingAgentID"),
        "comment": raw.get("Comment") or None,
        "create_date": _as_date(raw.get("CreateDate")),
        "update_date": _as_date(raw.get("UpdateDate")),
    }


def normalize_charge(raw: dict[str, Any]) -> dict[str, Any]:
    amount = _as_decimal(raw.get("Amount")) or 0.0
    allocated = _as_decimal(raw.get("AmountAllocated"))
    fully = raw.get("IsFullyAllocated")
    # RM often omits AmountAllocated + IsFullyAllocated on open-charge rows.
    # Missing allocated must NOT default to amount (that falsely zeros balances).
    if allocated is None:
        if fully is True:
            allocated = amount
        else:
            allocated = 0.0
    balance = round(amount - float(allocated), 2)
    if fully is None:
        if balance <= 0.009:
            fully = True
        elif allocated == 0.0 and amount > 0:
            fully = False
    return {
        "charge_id": raw.get("ChargeID") or raw.get("ID"),
        "account_id": raw.get("AccountID"),
        "account_type": raw.get("AccountType"),
        "amount": amount,
        "amount_allocated": allocated,
        "balance": balance,
        "is_fully_allocated": fully,
        "transaction_date": _as_date(raw.get("TransactionDate")),
        "property_id": raw.get("PropertyID"),
        "unit_id": raw.get("UnitID"),
        "charge_type_id": raw.get("ChargeTypeID"),
        "transaction_type": raw.get("TransactionType"),
        "deposit_status": raw.get("DepositStatus"),
    }


def normalize_payment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "payment_id": raw.get("PaymentID") or raw.get("ID"),
        "account_id": raw.get("AccountID"),
        "amount": _as_decimal(raw.get("Amount")),
        "amount_allocated": _as_decimal(raw.get("AmountAllocated")),
        "is_fully_allocated": raw.get("IsFullyAllocated"),
        "transaction_date": _as_date(raw.get("TransactionDate")),
        "reference": raw.get("Reference") or None,
        "comment": raw.get("Comment") or None,
        "transaction_type": raw.get("TransactionType"),
    }


def _parse_age_days(age: Any) -> float | None:
    """RM Age looks like days.hh:mm:ss."""
    if age is None:
        return None
    if isinstance(age, (int, float)):
        return float(age)
    s = str(age).strip()
    if not s:
        return None
    try:
        if "." in s and ":" in s:
            days_part, time_part = s.split(".", 1)
            days = float(days_part)
            hh, mm, ss = time_part.split(":")
            return days + (int(hh) / 24) + (int(mm) / 1440) + (float(ss) / 86400)
        if ":" in s:
            parts = s.split(":")
            if len(parts) == 3:
                hh, mm, ss = parts
                return (int(hh) / 24) + (int(mm) / 1440) + (float(ss) / 86400)
        return float(s)
    except (TypeError, ValueError):
        return None


def normalize_work_order(raw: dict[str, Any]) -> dict[str, Any]:
    status = raw.get("Status") if isinstance(raw.get("Status"), dict) else None
    vendor = raw.get("Vendor") if isinstance(raw.get("Vendor"), dict) else None
    assigned = raw.get("AssignedToUser") if isinstance(raw.get("AssignedToUser"), dict) else None

    props = raw.get("Properties") if isinstance(raw.get("Properties"), list) else []
    units = raw.get("Units") if isinstance(raw.get("Units"), list) else []
    tenants = raw.get("Tenants") if isinstance(raw.get("Tenants"), list) else []

    prop = _first_dict(props)
    unit = _first_dict(units)
    tenant = _first_dict(tenants)

    property_id = raw.get("PropertyID") or (prop or {}).get("PropertyID")
    unit_id = raw.get("UnitID") or (unit or {}).get("UnitID")
    tenant_id = raw.get("TenantID") or (tenant or {}).get("TenantID")

    return {
        "work_order_id": raw.get("ServiceManagerIssueID"),
        "title": raw.get("Title"),
        "description": raw.get("Description") or None,
        "resolution": raw.get("Resolution") or None,
        "is_closed": raw.get("IsClosed"),
        "status_id": raw.get("StatusID") or (status or {}).get("ServiceManagerStatusID"),
        "status_name": (status or {}).get("Name"),
        "category_id": raw.get("CategoryID"),
        "priority_id": raw.get("PriorityID"),
        "vendor_id": raw.get("VendorID") or (vendor or {}).get("VendorID"),
        "vendor_name": (vendor or {}).get("Name"),
        "assigned_to_user_id": raw.get("AssignedToUserID") or (assigned or {}).get("UserID"),
        "assigned_to_name": (assigned or {}).get("Username")
        or (assigned or {}).get("Name")
        or (assigned or {}).get("FirstName"),
        "property_id": property_id,
        "property_name": (prop or {}).get("Name"),
        "unit_id": unit_id,
        "unit_name": (unit or {}).get("Name"),
        "tenant_id": tenant_id,
        "tenant_name": (tenant or {}).get("Name"),
        "create_date": _as_date(raw.get("CreateDate")),
        "update_date": _as_date(raw.get("UpdateDate")),
        "close_date": _as_date(raw.get("CloseDate")),
        "age_days": _parse_age_days(raw.get("Age")),
        "note_text": raw.get("NoteText") or None,
        "customer_description": raw.get("CustomerDescription") or None,
    }

