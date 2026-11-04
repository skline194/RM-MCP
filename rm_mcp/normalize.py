"""Normalize raw Rent Manager payloads into stable DTOs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .client import UDF_ASKING_RENT, UDF_LIST_FOR_LEASE, UDF_LIST_ON_TENANT_TURNER

# KP recurring-charge codes. Match ChargeType.Name exactly, ignoring case.
# Edit this list. Do not match descriptions, comments, or substrings:
# RENTIN, RInsur, REF-Re, RP, STO, EQRC, and RAP are not rent.
# GL accounts are notes for editors. Classification does not read the GL.
RENT_CHARGE_CODES: dict[str, tuple[str, ...]] = {
    # Rent Charge, GL 4311 Rents Received. The tenant's own portion.
    "base_rent": ("RC",),
    # Subsidized Portion of Rent, GL 4314 Subsidized Income. Kept separate from RC.
    "subsidized_rent": ("RC8",),
    # Booked to GL 4311 Rents Received, but not base rent.
    "surcharge": (
        "RAC",  # Window AC unit surcharge
        "RWD",  # Washer/dryer surcharge
    ),
    # Rent Credit Incentive, GL 5020 Mgmt Advertising Expense. Amount is negative.
    "concession": ("RCI",),
}
RENT_CLASS_OTHER = "other"

_RENT_CODE_CLASS: dict[str, str] = {
    code.casefold(): rent_class
    for rent_class, codes in RENT_CHARGE_CODES.items()
    for code in codes
}


def rent_class_for_code(code: str | None) -> str:
    """Exact charge-type code, case-insensitive. Anything else is other."""
    if code is None:
        return RENT_CLASS_OTHER
    return _RENT_CODE_CLASS.get(str(code).strip().casefold(), RENT_CLASS_OTHER)


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


PERSON_ID_FIELDS = {
    "tenant": "TenantID",
    "prospect": "ProspectID",
    "owner": "OwnerID",
    "vendor": "VendorID",
    "contact": "ContactID",
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _primary_phone_number(numbers: Any) -> str | None:
    if isinstance(numbers, dict):
        numbers = [numbers]
    if not isinstance(numbers, list):
        return None
    first = None
    for item in numbers:
        if not isinstance(item, dict):
            continue
        number = _text(item.get("PhoneNumber") or item.get("StrippedPhoneNumber"))
        if not number:
            continue
        if item.get("IsPrimary") is True:
            return number
        if first is None:
            first = number
    return first


def _phone_from_person(raw: Any) -> str | None:
    """Phone from a person payload. Direct fields win, then primary PhoneNumbers."""
    if not isinstance(raw, dict):
        return None
    direct = _text(raw.get("Phone") or raw.get("MobilePhone") or raw.get("HomePhone"))
    if direct:
        return direct
    listed = _primary_phone_number(raw.get("PhoneNumbers"))
    if listed:
        return listed
    for key in ("PrimaryContact", "Contact"):
        found = _phone_from_person(raw.get(key))
        if found:
            return found
    contacts = raw.get("Contacts")
    if isinstance(contacts, list):
        for item in contacts:
            found = _phone_from_person(item)
            if found:
                return found
    return None


def _email_from_person(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    direct = _text(raw.get("Email") or raw.get("EcheckEmail"))
    if direct:
        return direct
    for key in ("PrimaryContact", "Contact"):
        found = _email_from_person(raw.get(key))
        if found:
            return found
    contacts = raw.get("Contacts")
    if isinstance(contacts, list):
        for item in contacts:
            found = _email_from_person(item)
            if found:
                return found
    return None


def _person_name(raw: dict[str, Any]) -> str | None:
    for key in ("Name", "DisplayName", "Payee"):
        text = _text(raw.get(key))
        if text:
            return text
    parts = [_text(raw.get(key)) for key in ("FirstName", "LastName")]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _person_status(kind: str, raw: dict[str, Any]) -> str | None:
    if kind == "prospect":
        return _text(raw.get("ProspectStatus") or raw.get("Status"))
    if kind in {"vendor", "contact"}:
        named = _text(raw.get("Status"))
        if named:
            return named
        active = raw.get("IsActive")
        if active is True:
            return "Active"
        if active is False:
            return "Inactive"
        return None
    return _text(raw.get("Status"))


def normalize_person(kind: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """Compact QuickSearch hit. Phone, email, and status are null when the payload has none."""
    id_field = PERSON_ID_FIELDS.get(kind)
    if id_field is None:
        return None
    ident = raw.get(id_field)
    if ident is None or ident == "":
        return None
    return {
        "kind": kind,
        "id": ident,
        "name": _person_name(raw),
        "phone": _phone_from_person(raw),
        "email": _email_from_person(raw),
        "status": _person_status(kind, raw),
        "property_id": raw.get("PropertyID"),
        "parent_id": raw.get("ParentID"),
        "parent_type": raw.get("ParentType"),
    }


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_phone_number(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "phone_number_id": raw.get("PhoneNumberID"),
        "phone_number": raw.get("PhoneNumber"),
        "extension": raw.get("Extension") or None,
        "is_primary": raw.get("IsPrimary"),
        "is_text_ready": raw.get("IsTextReady"),
        "parent_id": raw.get("ParentID"),
    }


def normalize_ownership(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_contract_id": raw.get("OwnerContractID"),
        "owner_id": raw.get("OwnerID"),
        "property_id": raw.get("PropertyID"),
        "contract_from": _as_date(raw.get("ContractFrom")),
        "contract_to": _as_date(raw.get("ContractTo")),
        "percentage": _as_decimal(raw.get("Percentage")),
        "reserve": _as_decimal(raw.get("Reserve")),
    }


def normalize_owner(
    raw: dict[str, Any],
    *,
    balance: float | None = None,
    balance_field: str | None = None,
    include_balance: bool = False,
) -> dict[str, Any]:
    phones = [normalize_phone_number(item) for item in _dict_list(raw.get("PhoneNumbers"))]
    addresses = []
    for item in _dict_list(raw.get("Addresses")):
        dto = normalize_address(item) or {}
        dto["address_id"] = item.get("AddressID")
        dto["is_primary"] = item.get("IsPrimary")
        dto["is_billing"] = item.get("IsBilling")
        addresses.append(dto)
    ownership_raw = raw.get("Ownerships")
    if not ownership_raw:
        ownership_raw = raw.get("OwnerContracts")
    dto = {
        "owner_id": raw.get("OwnerID"),
        "name": raw.get("Name") or raw.get("DisplayName"),
        "display_name": raw.get("DisplayName"),
        "status": raw.get("Status"),
        "email": _email_from_person(raw),
        "phone": _phone_from_person(raw),
        "comment": raw.get("Comment") or None,
        "phone_numbers": phones,
        "addresses": addresses,
        "ownerships": [normalize_ownership(item) for item in _dict_list(ownership_raw)],
    }
    if include_balance:
        dto["balance"] = balance
        dto["balance_field"] = balance_field
    return dto


def _gl_account_fields(charge_type: dict[str, Any]) -> tuple[Any, str | None]:
    """GL id and a readable label when the embedded ChargeType carries them.

    Classification does not use this. Field names vary, so missing pieces stay null.
    """
    gl = charge_type.get("GLAccount") if isinstance(charge_type.get("GLAccount"), dict) else {}
    gl_id = charge_type.get("GLAccountID")
    if gl_id is None:
        gl_id = gl.get("GLAccountID")
    number = _text(gl.get("Reference") or gl.get("AccountNumber") or charge_type.get("GLAccountReference"))
    name = _text(gl.get("Name") or charge_type.get("GLAccountName"))
    if number and name:
        label = f"{number} {name}"
    else:
        label = number or name
    return gl_id, label


def _charge_in_effect(from_date: str | None, to_date: str | None, today: date) -> bool:
    today_s = today.isoformat()
    if from_date and from_date > today_s:
        return False
    if to_date and to_date < today_s:
        return False
    return True


def normalize_recurring_charge(raw: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """One recurring charge. rent_class comes from ChargeType.Name, not the comment."""
    charge_type = raw.get("ChargeType") if isinstance(raw.get("ChargeType"), dict) else {}
    # Name is the short code (RC, RC8, RENTIN). Description is not a code.
    code = _text(charge_type.get("Name") or raw.get("ChargeTypeName"))
    description = _text(charge_type.get("Description") or raw.get("ChargeTypeDescription"))
    gl_id, gl_label = _gl_account_fields(charge_type)
    from_date = _as_date(raw.get("FromDate"))
    to_date = _as_date(raw.get("ToDate"))
    return {
        "recurring_charge_id": raw.get("RecurringChargeID"),
        "entity_type": raw.get("EntityType"),
        "entity_key_id": raw.get("EntityKeyID"),
        "tenant_id": raw.get("TenantID"),
        "unit_id": raw.get("UnitID"),
        "property_id": raw.get("PropertyID"),
        "charge_type_id": raw.get("ChargeTypeID") if raw.get("ChargeTypeID") is not None else charge_type.get("ChargeTypeID"),
        "charge_type_name": code,
        "charge_type_description": description,
        "gl_account_id": gl_id,
        "gl_account": gl_label,
        "amount": _as_decimal(raw.get("Amount")),
        "comment": _text(raw.get("Comment")),
        "from_date": from_date,
        "to_date": to_date,
        "frequency": raw.get("Frequency"),
        "is_calculated": raw.get("IsCalculated"),
        "is_current": _charge_in_effect(from_date, to_date, today or date.today()),
        "rent_class": rent_class_for_code(code),
    }


def normalize_open_receivable(raw: dict[str, Any]) -> dict[str, Any]:
    """Open receivable line. AmountDue is already the open amount and is not reduced again."""
    charge_type = raw.get("ChargeType") if isinstance(raw.get("ChargeType"), dict) else {}
    original = _as_decimal(raw.get("OriginalAmount"))
    paid = _as_decimal(raw.get("AmountPaid"))
    if paid is None:
        paid = _as_decimal(raw.get("AmountAllocated"))
    due = _as_decimal(raw.get("AmountDue"))
    if due is None:
        amount = _as_decimal(raw.get("Amount"))
        if amount is not None and paid is not None and original is None:
            due = round(amount - float(paid), 2)
        elif amount is not None:
            due = amount
        elif original is not None and paid is not None:
            due = round(float(original) - float(paid), 2)
        else:
            due = original
    description = _text(raw.get("ChargeTypeDescription") or charge_type.get("Description") or charge_type.get("Name"))
    return {
        "charge_id": raw.get("ChargeID") or raw.get("ID"),
        "account_id": raw.get("AccountID"),
        "account_name": raw.get("AccountName"),
        "transaction_date": _as_date(raw.get("TransactionDate") or raw.get("Date")),
        "property_id": raw.get("PropertyID"),
        "property_name": raw.get("PropertyName"),
        "unit_id": raw.get("UnitID"),
        "unit_name": raw.get("UnitName"),
        "charge_type_id": raw.get("ChargeTypeID") if raw.get("ChargeTypeID") is not None else charge_type.get("ChargeTypeID"),
        "charge_type_description": description,
        "description": _text(raw.get("Description") or raw.get("Comment")),
        "reference": _text(raw.get("Reference")),
        "comment": _text(raw.get("Comment")),
        "original_amount": original,
        "amount_paid": 0.0 if paid is None else paid,
        "amount_due": due,
        "balance": due,
    }


def normalize_prospect_card(raw: dict[str, Any]) -> dict[str, Any]:
    """Prospect list row. Stage, source, and phone come from embeds when present."""
    stage = raw.get("CurrentProspectStage") if isinstance(raw.get("CurrentProspectStage"), dict) else {}
    lead = raw.get("LeadSource") if isinstance(raw.get("LeadSource"), dict) else {}
    stage_id = raw.get("CurrentProspectStageID")
    if stage_id is None:
        stage_id = stage.get("ProspectStageID")
    lead_id = raw.get("LeadSourceID")
    if lead_id is None:
        lead_id = lead.get("ProspectLeadSourceID")
    return {
        "prospect_id": raw.get("ProspectID"),
        "display_id": raw.get("ProspectDisplayID"),
        "property_id": raw.get("PropertyID"),
        "name": _text(raw.get("Name")) or _person_name(raw),
        "first_name": raw.get("FirstName"),
        "last_name": raw.get("LastName"),
        "status": _text(raw.get("ProspectStatus") or raw.get("Status")),
        "stage_id": stage_id,
        "stage_name": _text(stage.get("Name")),
        "lead_source_id": lead_id,
        "lead_source": _text(lead.get("Description") or lead.get("Name")),
        "leasing_agent_id": raw.get("LeasingAgentID"),
        "phone": _phone_from_person(raw),
        "email": _email_from_person(raw),
        "comment": _text(raw.get("Comment")),
        "application_date": _as_date(raw.get("ApplicationDate")),
        "expected_move_in": _as_date(raw.get("MoveInDate") or raw.get("ReservedMoveIn")),
        "first_contact": _as_date(raw.get("FirstContact")),
        "last_contact": _as_date(raw.get("LastContact")),
        "created": _as_date(raw.get("CreateDate")),
        "updated": _as_date(raw.get("UpdateDate")),
        "rent_min": raw.get("RentMin"),
        "rent_max": raw.get("RentMax"),
        "people_count": raw.get("PeopleCount"),
        "interest_level": raw.get("InterestLevel"),
        "lost_reason_id": raw.get("ProspectLostReasonID"),
        "reserved_unit_id": raw.get("ReservedUnitID"),
    }


def normalize_prospect_contact(raw: dict[str, Any]) -> dict[str, Any]:
    dto = normalize_contact(raw)
    if not dto.get("phone"):
        dto["phone"] = _phone_from_person(raw)
    if not dto.get("email"):
        dto["email"] = _email_from_person(raw)
    return dto


def normalize_prospect_address(raw: dict[str, Any]) -> dict[str, Any]:
    dto = normalize_address(raw) or {}
    dto["address_id"] = raw.get("AddressID")
    dto["is_primary"] = raw.get("IsPrimary")
    dto["is_billing"] = raw.get("IsBilling")
    return dto


def normalize_interested_rental(raw: dict[str, Any]) -> dict[str, Any]:
    unit = raw.get("Unit") if isinstance(raw.get("Unit"), dict) else {}
    prop = raw.get("Property") if isinstance(raw.get("Property"), dict) else {}
    entity = _text(raw.get("EntityTypeID") or raw.get("EntityType"))
    entity_key = str(entity or "").casefold()
    unit_id = _as_int(raw.get("UnitID"))
    if unit_id is None:
        unit_id = _as_int(unit.get("UnitID"))
    if unit_id is None and entity_key == "unit":
        unit_id = _as_int(raw.get("EntityKeyID"))
    property_id = _as_int(raw.get("PropertyID"))
    if property_id is None:
        property_id = _as_int(prop.get("PropertyID"))
    if property_id is None and entity_key == "property":
        property_id = _as_int(raw.get("EntityKeyID"))
    return {
        "interested_rental_id": raw.get("InterestedRentalID") or raw.get("ProspectWaitingListID"),
        "unit_id": unit_id,
        "unit_name": _text(unit.get("Name")),
        "property_id": property_id,
        "property_name": _text(prop.get("Name")),
        "is_waiting_list": raw.get("IsWaitingList"),
        "created": _as_date(raw.get("CreateDate")),
    }


def normalize_stage_history(raw: dict[str, Any]) -> dict[str, Any]:
    stage = raw.get("ProspectStage") if isinstance(raw.get("ProspectStage"), dict) else {}
    stage_id = raw.get("ProspectStageID")
    if stage_id is None:
        stage_id = stage.get("ProspectStageID")
    return {
        "history_id": raw.get("ProspectStageHistoryID"),
        "prospect_id": raw.get("ProspectID"),
        "stage_id": stage_id,
        "stage_name": _text(stage.get("Name")),
        "from_date": _text(raw.get("FromDate")),
        "to_date": _text(raw.get("ToDate")),
        "from_day": _as_date(raw.get("FromDate")),
        "to_day": _as_date(raw.get("ToDate")),
    }


def normalize_lost_reason(
    raw: dict[str, Any] | None,
    *,
    fallback_id: Any = None,
    fallback_description: Any = None,
) -> dict[str, Any] | None:
    source = raw if isinstance(raw, dict) else {}
    lost_id = source.get("ProspectLostReasonID")
    if lost_id is None:
        lost_id = fallback_id
    name = _text(source.get("Name"))
    description = _text(source.get("Description") or fallback_description)
    if lost_id is None and not name and not description:
        return None
    return {
        "lost_reason_id": lost_id,
        "name": name,
        "description": description,
        "prospect_status": _text(source.get("ProspectStatus")),
        "is_active": source.get("IsActive") if isinstance(raw, dict) else None,
    }


def normalize_prospect_stage(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage_id": raw.get("ProspectStageID"),
        "name": raw.get("Name"),
        "description": _text(raw.get("Description")),
        "is_active": raw.get("IsActive"),
        "sort_order": raw.get("SortOrder"),
        "follow_up_days": raw.get("FollowUpDays"),
    }


def normalize_lost_reason_lookup(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "lost_reason_id": raw.get("ProspectLostReasonID"),
        "name": raw.get("Name"),
        "description": _text(raw.get("Description")),
        "prospect_status": _text(raw.get("ProspectStatus")),
        "is_active": raw.get("IsActive"),
        "sort_order": raw.get("SortOrder"),
    }


def normalize_lead_source(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "lead_source_id": raw.get("ProspectLeadSourceID"),
        "name": _text(raw.get("Description") or raw.get("Name")),
        "is_active": raw.get("IsActive"),
        "phone": _text(raw.get("PhoneNumber")),
    }


def normalize_user_brief(raw: dict[str, Any]) -> dict[str, Any]:
    """Staff directory row. Password, tokens, and partner fields are not copied."""
    first = _text(raw.get("Firstname") or raw.get("FirstName"))
    last = _text(raw.get("Lastname") or raw.get("LastName"))
    name = _text(raw.get("Name"))
    if not name:
        name = " ".join(part for part in (first, last) if part) or _text(raw.get("Username"))
    return {
        "user_id": raw.get("UserID"),
        "username": _text(raw.get("Username")),
        "name": name,
        "first_name": first,
        "last_name": last,
        "email": _text(raw.get("Email")),
        "is_active": raw.get("IsActive"),
        "is_leasing_agent": raw.get("IsLeasingAgent"),
        "is_group": raw.get("IsGroup"),
    }


def _linked_name(raw: dict[str, Any], key: str) -> str | None:
    linked = raw.get(key)
    if isinstance(linked, dict):
        return _text(linked.get("Name")) or _person_name(linked)
    return None


def _linked_property_id(raw: dict[str, Any]) -> Any:
    for key in ("Tenant", "Prospect"):
        linked = raw.get(key)
        if isinstance(linked, dict) and linked.get("PropertyID") is not None:
            return linked.get("PropertyID")
    return None


def _assigned_user_ids(raw: dict[str, Any], key: str) -> list[Any]:
    users = raw.get(key)
    if isinstance(users, dict):
        users = [users]
    if not isinstance(users, list):
        return []
    ids = []
    for item in users:
        if isinstance(item, dict) and item.get("UserID") is not None:
            ids.append(item.get("UserID"))
    return ids


def normalize_appointment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "appointment_id": raw.get("AppointmentID"),
        "subject": _text(raw.get("Subject")),
        "description": _text(raw.get("Description")),
        "starts_at": _text(raw.get("StartDate")),
        "ends_at": _text(raw.get("EndDate")),
        "start_date": _as_date(raw.get("StartDate")),
        "end_date": _as_date(raw.get("EndDate")),
        "is_reminder": raw.get("IsReminder"),
        "is_recurring": raw.get("IsRecurring"),
        "tenant_id": raw.get("TenantID"),
        "tenant_name": _linked_name(raw, "Tenant"),
        "prospect_id": raw.get("ProspectID"),
        "prospect_name": _linked_name(raw, "Prospect"),
        "owner_id": raw.get("OwnerID"),
        "vendor_id": raw.get("VendorID"),
        "property_id": _linked_property_id(raw),
        "service_issue_id": raw.get("ServiceManagerIssueID") or raw.get("IssueID"),
        "assigned_user_ids": _assigned_user_ids(raw, "AppointmentUsers"),
    }


def normalize_task(raw: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    close_day = _as_date(raw.get("CloseDate"))
    is_closed = raw.get("IsClosed") is True or bool(close_day)
    due_day = _as_date(raw.get("DueDate"))
    today_s = (today or date.today()).isoformat()
    is_overdue = bool(due_day) and due_day < today_s and not is_closed
    return {
        "task_id": raw.get("TaskID"),
        "subject": _text(raw.get("Subject")),
        "description": _text(raw.get("Description")),
        "due_at": _text(raw.get("DueDate")),
        "due_date": due_day,
        "to_date": _as_date(raw.get("ToDate")),
        "is_closed": is_closed,
        "close_date": close_day,
        "is_overdue": is_overdue,
        "location": _text(raw.get("Location")),
        "action_type": _text(raw.get("ActionType")),
        "tenant_id": raw.get("TenantID"),
        "tenant_name": _linked_name(raw, "Tenant"),
        "prospect_id": raw.get("ProspectID"),
        "prospect_name": _linked_name(raw, "Prospect"),
        "service_issue_id": raw.get("ServiceManagerIssueID"),
        "assigned_user_ids": _assigned_user_ids(raw, "TaskUsers"),
        "created": _as_date(raw.get("CreateDate")),
    }


def normalize_lease_renewal(raw: dict[str, Any]) -> dict[str, Any]:
    term = raw.get("LeaseTerm") if isinstance(raw.get("LeaseTerm"), dict) else {}
    term_name = _text(term.get("Name") or term.get("LeaseTermName") or term.get("Term"))
    return {
        "lease_renewal_id": raw.get("LeaseRenewalID"),
        "lease_id": raw.get("ParentLeaseID"),
        "unit_id": raw.get("UnitID"),
        "lease_term_id": raw.get("LeaseTermID"),
        "lease_term": term_name,
        "lease_length": raw.get("LeaseLength"),
        "sign_date": _as_date(raw.get("SignDate")),
        "start_date": _as_date(raw.get("StartDate")),
        "end_date": _as_date(raw.get("EndDate")),
        "signable_document_status": _text(raw.get("SignableDocumentStatus")),
    }


def normalize_increase_offer(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Offer row. offer_status is inferred: ApprovalDate means approved, otherwise set.

    LeaseRenewalIncreaseOffer has no status enum in the help model.
    """
    if not isinstance(raw, dict):
        return None
    interesting = ("LeaseRenewalIncreaseOfferID", "Amount", "ChargeIncrease", "SetDate", "ApprovalDate")
    if not any(raw.get(key) not in (None, "") for key in interesting):
        return None
    approval = _as_date(raw.get("ApprovalDate"))
    set_date = _as_date(raw.get("SetDate"))
    if approval:
        status = "approved"
    elif set_date or raw.get("Amount") is not None or raw.get("ChargeIncrease") is not None:
        status = "set"
    else:
        status = None
    return {
        "offer_id": raw.get("LeaseRenewalIncreaseOfferID"),
        "amount": _as_decimal(raw.get("Amount")),
        "charge_increase": _as_decimal(raw.get("ChargeIncrease")),
        "charge_type_id": raw.get("ChargeTypeID"),
        "recurring_charge_id": raw.get("RecurringChargeID"),
        "set_date": set_date,
        "approval_date": approval,
        "comment": _text(raw.get("Comment")),
        "offer_status": status,
    }


def _report_category(raw: dict[str, Any]) -> tuple[Any, str | None]:
    report = raw.get("Report") if isinstance(raw.get("Report"), dict) else {}
    cat = raw.get("ReportCategory") if isinstance(raw.get("ReportCategory"), dict) else {}
    if not cat and isinstance(report.get("ReportCategory"), dict):
        cat = report["ReportCategory"]
    category_id = raw.get("ReportCategoryID")
    if category_id is None:
        category_id = report.get("ReportCategoryID")
    if category_id is None:
        category_id = cat.get("ReportCategoryID")
    return category_id, _text(cat.get("Name") or cat.get("Description"))


def normalize_report_listing(raw: dict[str, Any]) -> dict[str, Any]:
    category_id, category_name = _report_category(raw)
    return {
        "kind": "report",
        "report_id": raw.get("ReportID"),
        "name": _text(raw.get("Name")),
        "description": _text(raw.get("Description")),
        "category_id": category_id,
        "category_name": category_name,
        "hide_grid_view": raw.get("HideGridView"),
    }


def normalize_memorized_report(raw: dict[str, Any]) -> dict[str, Any]:
    report = raw.get("Report") if isinstance(raw.get("Report"), dict) else {}
    category_id, category_name = _report_category(raw)
    return {
        "kind": "memorized",
        "memorized_report_id": raw.get("MemorizedReportID"),
        "name": _text(raw.get("Name")),
        "report_id": raw.get("ReportID"),
        "description": _text(report.get("Description") or report.get("Name")),
        "category_id": category_id,
        "category_name": category_name,
        "excluded_from_menu": raw.get("IsExcludedFromMenu"),
    }


def normalize_report_parameter(raw: dict[str, Any]) -> dict[str, Any]:
    """One report parameter.

    `parameter` is the token to send back to RunReport. The help sample puts a
    numeric ReportParameterID ("0") on the nested object and a PropertyName
    string beside it, so a non-numeric ReportParameterID wins, then PropertyName.
    """
    nested = raw.get("ReportParameter") if isinstance(raw.get("ReportParameter"), dict) else {}
    parameter_id = raw.get("ReportParameterID")
    if parameter_id is None:
        parameter_id = nested.get("ReportParameterID")
    token = None
    for candidate in (
        raw.get("ReportParameterID"),
        nested.get("ReportParameterID"),
        raw.get("Name"),
        nested.get("Name"),
    ):
        text = _text(candidate)
        if text and not text.isdigit():
            token = text
            break
    if token is None:
        token = _text(
            raw.get("PropertyName")
            or nested.get("PropertyName")
            or raw.get("DisplayName")
            or nested.get("DisplayName")
        )
    return {
        "parameter": token,
        "parameter_id": parameter_id,
        "display_name": _text(raw.get("DisplayName") or nested.get("DisplayName") or nested.get("Name")),
        "prompt": _text(raw.get("PromptText") or nested.get("PromptText")),
        "data_type": _text(raw.get("DataType") or nested.get("DataType")),
        "property_name": _text(raw.get("PropertyName") or nested.get("PropertyName")),
        "default_value": _text(raw.get("DefaultValue") or nested.get("DefaultValue")),
        "is_hidden": raw.get("IsHidden") if raw.get("IsHidden") is not None else nested.get("IsHidden"),
        "sort_order": raw.get("SortOrder") if raw.get("SortOrder") is not None else nested.get("SortOrder"),
        "tooltip": _text(raw.get("ToolTip") or nested.get("ToolTip")),
    }


def normalize_saved_report_value(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "saved_value_id": raw.get("MemorizedReportSavedReportParameterValueID")
        or raw.get("ReportSavedParameterID"),
        "value": _text(raw.get("ReportParameterValue")),
        "is_prompt": raw.get("IsPrompt"),
        "memorized_report_id": raw.get("MemorizedReportID"),
    }


def normalize_history_event(event_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    when = raw.get("HistoryDate")
    text = str(when).strip() if when not in (None, "") else ""
    attachments = raw.get("HistoryAttachments")
    has_attachment = bool(raw.get("FileID")) or (isinstance(attachments, list) and len(attachments) > 0)
    return {
        "type": event_type,
        "history_id": raw.get("HistoryID"),
        "history_date": text or None,
        "note": raw.get("Note") or None,
        "result": raw.get("Result") or None,
        "parent_id": raw.get("ParentID"),
        "history_category_id": raw.get("HistoryCategoryID"),
        "is_completed": raw.get("IsCompleted"),
        "is_contact_made": raw.get("IsContactMade"),
        "is_pinned": raw.get("IsPinned"),
        "has_attachment": has_attachment,
        "history_type": raw.get("HistoryType"),
    }

