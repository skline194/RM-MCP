"""Guarded Rent Manager write helpers.

Phase B contract:
- dry_run=True by default at tool layer
- live writes require approved_by
- allowlisted UDF field IDs only
- audit every attempt to /tmp/rm_mcp_write_audit.jsonl
- prefer known-good endpoints/payloads from live Lucy probes
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .client import RMClient

AUDIT_PATH = Path("/tmp/rm_mcp_write_audit.jsonl")
CATALOG = Path(__file__).resolve().parent / "catalog" / "write_matrix.json"

ALLOWLISTED_UDF_FIELD_IDS = {710, 731, 732}
YES_NO_UDF_FIELD_IDS = {710, 731}
NOTE_ENTITY_TYPES = {"Tenant", "Unit", "Property", "Vendor", "ServiceManagerIssue", "Note"}


def load_write_matrix() -> dict[str, Any]:
    if CATALOG.exists():
        return json.loads(CATALOG.read_text())
    return {}


def _audit(event: dict[str, Any]) -> str:
    event = {**event, "ts": time.time(), "audit_id": event.get("audit_id") or str(uuid.uuid4())}
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    return str(event["audit_id"])


def classify_write_error(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", None) or str(exc)
    msg = str(exc)
    low = (str(body) or msg).lower()
    code = "write_failed"
    if status == 401:
        code = "auth_failed"
    elif status == 403 or "insufficient privileges" in low or "required privilege" in low:
        code = "write_not_permitted"
    elif status == 405:
        code = "method_not_allowed"
    elif status == 404:
        code = "endpoint_not_found"
    elif status == 500 and "privilege" in low:
        code = "write_not_permitted"
    elif status == 500 and (
        "must not be blank" in low or "can not be blank" in low or "cannot be blank" in low
    ):
        code = "invalid_payload"
    return {"code": code, "status": status, "message": msg[:400], "body": str(body)[:400]}


def extract_entity_ids(payload: Any) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        return ids
    keys = (
        "HistoryID",
        "ServiceManagerIssueID",
        "UserDefinedValueID",
        "NoteID",
        "TaskID",
        "FileID",
        "PhoneNumberID",
        "ContactID",
        "ImageID",
        "UnitID",
        "TenantID",
        "PropertyID",
        "ParentID",
        "UserDefinedFieldID",
        "ID",  # Bills primary key
        "BillID",
        "BillDetailID",
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in keys:
            if k in row and row.get(k) is not None and k not in ids:
                ids[k] = row.get(k)
    return ids


def normalize_yes_no(value: str) -> str:
    low = str(value).strip().lower()
    if low in {"true", "1", "y", "yes"}:
        return "Yes"
    if low in {"false", "0", "n", "no"}:
        return "No"
    return str(value)


def build_add_note_plan(
    *,
    entity_type: str,
    parent_id: int,
    note: str,
    name: str | None = None,
    history_category_id: int | None = None,
    history_date: str | None = None,
) -> list[dict[str, Any]]:
    """Build ordered request plans using known-good endpoints only.

    Working live paths:
    - ServiceManagerIssue history: POST /ServiceManagerIssues/{id}/History
    - Tenant/Unit/Property/Vendor history notes: POST /{Entity}s/{id}/HistoryNotes
    - Generic Notes/Items: POST /Notes with required Name

    Non-working on this account/API:
    - POST /Tenants|Units|Properties|Vendors/{id}/History -> 405
    - PATCH/PUT /ServiceManagerIssues/{id} -> 405 (use collection POST upsert)
    - POST /History -> 404

    Vendor file attach (RM support 2026-09-27): no direct vendor documents
    resource. Use HistoryNotes + /HistoryNotes/{id}/UploadAttachment (same as
    other entities). Option A one-request multipart also exists on
    /Vendors/{id}/HistoryNotes?embeds=HistoryAttachments,HistoryAttachments.File
    """
    et = entity_type.strip()
    pid = int(parent_id)
    text = note.strip()
    subject = (name or "").strip() or text[:80] or f"note-{et}-{pid}"
    cat = int(history_category_id) if history_category_id is not None else None
    hdate = (history_date or "").strip() or None

    if et == "ServiceManagerIssue":
        body_wo: dict[str, Any] = {
            "HistoryType": "Note",
            "ParentID": pid,
            "EntityType": "ServiceManagerIssue",
            "Note": text,
        }
        if cat is not None:
            body_wo["HistoryCategoryID"] = cat
        if hdate:
            body_wo["HistoryDate"] = hdate
        return [
            {
                "method": "POST",
                "path": f"/ServiceManagerIssues/{pid}/History",
                "json_body": body_wo,
            }
        ]

    if et == "Note":
        body: dict[str, Any] = {"Name": subject}
        if text and text != subject:
            body["Description"] = text
            body["Note"] = text
        return [{"method": "POST", "path": "/Notes", "json_body": body}]

    # Tenant/Unit/Property/Vendor: /History POST is 405. /HistoryNotes POST is the path.
    plural = {
        "Tenant": "Tenants",
        "Unit": "Units",
        "Property": "Properties",
        "Vendor": "Vendors",
    }.get(et)
    if not plural:
        raise ValueError(f"unsupported note entity_type: {et}")

    # Vendor support sample is lean (Note + optional HistoryCategoryID/HistoryDate).
    # Keep HistoryType/ParentID/EntityType for Tenant/Unit/Property (already live-ok).
    if et == "Vendor":
        body_v: dict[str, Any] = {"Note": text}
        if cat is not None:
            body_v["HistoryCategoryID"] = cat
        if hdate:
            body_v["HistoryDate"] = hdate
        return [
            {
                "method": "POST",
                "path": f"/Vendors/{pid}/HistoryNotes",
                "json_body": body_v,
            }
        ]

    body_e: dict[str, Any] = {
        "HistoryType": "Note",
        "ParentID": pid,
        "EntityType": et,
        "Note": text,
    }
    if cat is not None:
        body_e["HistoryCategoryID"] = cat
    if hdate:
        body_e["HistoryDate"] = hdate
    return [
        {
            "method": "POST",
            "path": f"/{plural}/{pid}/HistoryNotes",
            "json_body": body_e,
        }
    ]



def build_update_unit_udf_plan(*, unit_id: int, field_id: int, value: str) -> list[dict[str, Any]]:
    fid = int(field_id)
    val = normalize_yes_no(value) if fid in YES_NO_UDF_FIELD_IDS else str(value)
    return [
        {
            "method": "POST",
            "path": f"/Units/{int(unit_id)}/UserDefinedValues",
            "json_body": {"UserDefinedFieldID": fid, "Value": val},
        }
    ]


def build_create_work_order_plan(
    *,
    title: str,
    description: str = "",
    property_id: int | None = None,
    unit_id: int | None = None,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"Title": title.strip(), "Description": description or ""}
    if property_id is not None:
        body["PropertyID"] = int(property_id)
    if unit_id is not None:
        body["UnitID"] = int(unit_id)
    if tenant_id is not None:
        body["TenantID"] = int(tenant_id)
    return [{"method": "POST", "path": "/ServiceManagerIssues", "json_body": body}]


# Fields that collection-POST upsert can safely mutate on this account.
# PATCH/PUT /ServiceManagerIssues/{id} is 405; updates go through POST /ServiceManagerIssues
# with ServiceManagerIssueID set. Omitted scalar fields may be cleared, so callers should
# merge against a current snapshot before sending.
WO_UPSERT_SCALAR_FIELDS = (
    "Title",
    "Description",
    "Resolution",
    "CustomerDescription",
    "NoteText",
    "StatusID",
    "AssignedToUserID",
)


def work_order_upsert_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a merge-safe ServiceManagerIssues upsert body from a GET payload."""
    if not isinstance(raw, dict):
        raise ValueError("work order payload must be a dict")
    wid = raw.get("ServiceManagerIssueID")
    if wid is None:
        raise ValueError("ServiceManagerIssueID required for WO upsert snapshot")
    status = raw.get("Status") if isinstance(raw.get("Status"), dict) else {}
    assigned = raw.get("AssignedToUser") if isinstance(raw.get("AssignedToUser"), dict) else {}
    status_id = raw.get("StatusID")
    if status_id is None:
        status_id = status.get("ServiceManagerStatusID")
    assigned_id = raw.get("AssignedToUserID")
    if assigned_id is None:
        assigned_id = assigned.get("UserID")
    body: dict[str, Any] = {
        "ServiceManagerIssueID": int(wid),
        "Title": raw.get("Title") or "",
        "Description": raw.get("Description") or "",
        "Resolution": raw.get("Resolution") or "",
        "CustomerDescription": raw.get("CustomerDescription") or "",
        "NoteText": raw.get("NoteText") or "",
    }
    if status_id is not None:
        body["StatusID"] = int(status_id)
    # UserID 0 is RM's <Unassigned> sentinel; still send so updates can clear assignment.
    if assigned_id is not None:
        body["AssignedToUserID"] = int(assigned_id)
    return body


def build_update_work_order_plan(
    *,
    work_order_id: int,
    current: dict[str, Any],
    title: str | None = None,
    description: str | None = None,
    resolution: str | None = None,
    customer_description: str | None = None,
    note_text: str | None = None,
    status_id: int | None = None,
    assigned_to_user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Update an existing WO via collection POST upsert (not PATCH/PUT).

    Always starts from current snapshot so omitted fields are preserved.
    Close/IsClosed is a HARD NO-GO for agents (Steve 2026-09-21: WOs billable; premature close costs money). Do not implement.
    """
    body = work_order_upsert_snapshot(current)
    if int(body["ServiceManagerIssueID"]) != int(work_order_id):
        raise ValueError("current snapshot ServiceManagerIssueID mismatch")
    if title is not None:
        body["Title"] = str(title).strip()
    if description is not None:
        body["Description"] = str(description)
    if resolution is not None:
        body["Resolution"] = str(resolution)
    if customer_description is not None:
        body["CustomerDescription"] = str(customer_description)
    if note_text is not None:
        body["NoteText"] = str(note_text)
    if status_id is not None:
        body["StatusID"] = int(status_id)
    if assigned_to_user_id is not None:
        body["AssignedToUserID"] = int(assigned_to_user_id)
    if not (body.get("Title") or "").strip():
        raise ValueError("Title cannot be blank on WO update")
    return [{"method": "POST", "path": "/ServiceManagerIssues", "json_body": body}]



def build_link_work_order_plan(
    *,
    work_order_id: int,
    unit_ids: list[int] | None = None,
    property_ids: list[int] | None = None,
    tenant_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Link unit/property/tenant IDs onto an existing WO.

    Working live paths (append semantics, not replace):
      POST /ServiceManagerIssues/{id}/LinkUnits        body: [UnitID, ...]
      POST /ServiceManagerIssues/{id}/LinkProperties   body: [PropertyID, ...]
      POST /ServiceManagerIssues/{id}/LinkTenants      body: [TenantID, ...]

    Body MUST be a raw JSON array of ints (objects/dicts return 400).
    Nested POST/PUT on .../Units|Properties|Tenants is 405.
    Collection upsert with UnitID/PropertyID/TenantID does not stick.
    Unlink* routes are documented in Help but 404 on this account.
    """
    wid = int(work_order_id)
    plan: list[dict[str, Any]] = []

    def _ids(values: list[int] | None, label: str) -> list[int]:
        if not values:
            return []
        out: list[int] = []
        seen: set[int] = set()
        for v in values:
            i = int(v)
            if i in seen:
                continue
            seen.add(i)
            out.append(i)
        if not out:
            raise ValueError(f"{label} produced no IDs")
        return out

    u = _ids(unit_ids, "unit_ids")
    p = _ids(property_ids, "property_ids")
    t = _ids(tenant_ids, "tenant_ids")
    if not (u or p or t):
        raise ValueError("pass at least one of unit_ids, property_ids, tenant_ids")

    if u:
        plan.append({
            "method": "POST",
            "path": f"/ServiceManagerIssues/{wid}/LinkUnits",
            "json_body": u,
        })
    if p:
        plan.append({
            "method": "POST",
            "path": f"/ServiceManagerIssues/{wid}/LinkProperties",
            "json_body": p,
        })
    if t:
        plan.append({
            "method": "POST",
            "path": f"/ServiceManagerIssues/{wid}/LinkTenants",
            "json_body": t,
        })
    return plan


def build_create_task_plan(
    *,
    subject: str,
    description: str = "",
    tenant_id: int | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
) -> list[dict[str, Any]]:
    # RM Tasks require Subject (Title is ignored / treated blank).
    body: dict[str, Any] = {
        "Subject": subject.strip(),
        "Description": description or "",
    }
    if tenant_id is not None:
        body["TenantID"] = int(tenant_id)
    if property_id is not None:
        body["PropertyID"] = int(property_id)
    if unit_id is not None:
        body["UnitID"] = int(unit_id)
    return [{"method": "POST", "path": "/Tasks", "json_body": body}]



# ---- File / image uploads (from RM support Postman collection 2026-09-16) ----
# Unit gallery:  POST /Units/{UnitID}/UploadImage  multipart
#   text Payload = [{"File":{"MetaTag":"Image1"},"ImageTypeID":1}]
#   file field name must match MetaTag
# History attachment (WO path): POST /HistoryNotes/{HistoryID}/UploadAttachment multipart
#   text File = {"HistoryID": <id>}
#   binary file field (name flexible; we use Attachment)
# These are progress toward WO photos; unit image != WO attachment.


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(suffix, "application/octet-stream")


def resolve_upload_file(
    *,
    file_path: str,
    max_bytes: int = 25 * 1024 * 1024,
) -> dict[str, Any]:
    """Validate a local file path for RM multipart upload.

    Returns metadata only (no bytes) for dry_run plans; caller opens file on live send.
    """
    raw = (file_path or "").strip()
    if not raw:
        raise ValueError("file_path is required")
    p = Path(raw).expanduser()
    if not p.is_file():
        raise ValueError(f"file not found: {p}")
    size = p.stat().st_size
    if size <= 0:
        raise ValueError(f"file is empty: {p}")
    if size > int(max_bytes):
        raise ValueError(f"file too large ({size} bytes); max {max_bytes}")
    name = p.name
    return {
        "path": str(p.resolve()),
        "filename": name,
        "size_bytes": size,
        "content_type": _guess_mime(p),
    }


def build_upload_unit_image_plan(
    *,
    unit_id: int,
    file_path: str,
    image_type_id: int = 1,
    meta_tag: str = "Image1",
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """POST /Units/{id}/UploadImage multipart (unit gallery, not WO)."""
    uid = int(unit_id)
    if uid <= 0:
        raise ValueError("unit_id must be positive")
    tag = (meta_tag or "Image1").strip() or "Image1"
    # MetaTag must be a safe form field name
    if not tag.replace("_", "").isalnum():
        raise ValueError("meta_tag must be alphanumeric/underscore")
    info = resolve_upload_file(file_path=file_path)
    payload = [{"File": {"MetaTag": tag}, "ImageTypeID": int(image_type_id)}]
    headers: dict[str, str] = {}
    if location_id is not None:
        headers["X-RM12Api-LocationID"] = str(int(location_id))
    return [
        {
            "method": "POST",
            "path": f"/Units/{uid}/UploadImage",
            "multipart": {
                "data_fields": {
                    "Payload": json.dumps(payload),
                },
                "file_fields": {
                    tag: {
                        "path": info["path"],
                        "filename": info["filename"],
                        "content_type": info["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": info,
            "notes": [
                "Unit gallery upload from RM support Postman collection",
                "Not a work-order attachment path",
            ],
        }
    ]


def build_upload_history_attachment_plan(
    *,
    history_id: int,
    file_path: str,
    location_id: int | None = None,
    file_field_name: str = "Attachment",
) -> list[dict[str, Any]]:
    """POST /HistoryNotes/{id}/UploadAttachment multipart.

    Preferred WO photo path once a history note exists on the work order.
    Postman example used text field key 'File' with JSON {"HistoryID": N}
    plus a binary file part.
    """
    hid = int(history_id)
    if hid <= 0:
        raise ValueError("history_id must be positive")
    info = resolve_upload_file(file_path=file_path)
    field = (file_field_name or "Attachment").strip() or "Attachment"
    headers: dict[str, str] = {}
    if location_id is not None:
        headers["X-RM12Api-LocationID"] = str(int(location_id))
    return [
        {
            "method": "POST",
            "path": f"/HistoryNotes/{hid}/UploadAttachment",
            "multipart": {
                "data_fields": {
                    # Support sample: key "File" with JSON body tying HistoryID
                    "File": json.dumps({"HistoryID": hid}),
                },
                "file_fields": {
                    field: {
                        "path": info["path"],
                        "filename": info["filename"],
                        "content_type": info["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": info,
            "notes": [
                "History note attachment — use after rm_add_note on a WO",
                "This is the likely work-order photo path",
            ],
        }
    ]


def build_attach_work_order_photo_plan(
    *,
    work_order_id: int,
    file_path: str,
    note: str = "",
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """Two-step WO photo plan: create history note, then attach file.

    Step 1 is JSON (existing WO history path).
    Step 2 is multipart UploadAttachment and needs HistoryID from step 1.
    guarded_write will stitch HistoryID into step 2 on live runs.
    """
    wid = int(work_order_id)
    if wid <= 0:
        raise ValueError("work_order_id must be positive")
    info = resolve_upload_file(file_path=file_path)
    text_note = (note or "").strip() or f"Photo attachment: {info['filename']}"
    step1 = {
        "method": "POST",
        "path": f"/ServiceManagerIssues/{wid}/History",
        "json_body": {
            "HistoryType": "Note",
            "ParentID": wid,
            "EntityType": "ServiceManagerIssue",
            "Note": text_note,
        },
    }
    # Placeholder history_id=0 — replaced at send time from step1 HistoryID
    step2 = {
        "method": "POST",
        "path": "/HistoryNotes/{history_id}/UploadAttachment",
        "path_template": True,
        "needs_history_id_from_prior_step": True,
        "multipart": {
            "data_fields": {
                "File": json.dumps({"HistoryID": "{history_id}"}),
            },
            "file_fields": {
                "Attachment": {
                    "path": info["path"],
                    "filename": info["filename"],
                    "content_type": info["content_type"],
                }
            },
        },
        "extra_headers": (
            {"X-RM12Api-LocationID": str(int(location_id))} if location_id is not None else None
        ),
        "file_meta": info,
        "notes": [
            "WO photo = history note + HistoryNotes UploadAttachment",
            "history_id filled from step 1 response on live send",
        ],
    }
    return [step1, step2]


# ---- Vendor Bills (RM API Support 2026-09-27) ----
# Create: POST /Bills?embeds=BillDetails (JSON array; Amount from details)
# Search: GET /Bills/Search filterExpression=AccountID,eq,... (NOT VendorID)
# Attach create-time: multipart text part Body + MetaTag file parts
# Attach existing multi: POST /Bills/{id}/FileAttachments text part Payload
# Attach existing single: POST /Bills/{id}/UploadAttachment file part file
# Update: POST upsert with fields= mask (no PUT/PATCH)
# Privilege: Payables > Bills > Add/View/Edit (+ property access).

def _as_positive_int(value: Any, label: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if n <= 0:
        raise ValueError(f"{label} must be positive")
    return n

def _as_money(value: Any, label: str = "amount") -> float:
    try:
        amt = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if amt <= 0:
        raise ValueError(f"{label} must be greater than 0")
    return round(amt, 2)

def _as_date_str(value: Any, label: str) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValueError(f"{label} is required (YYYY-MM-DD)")
    if "T" in s:
        s = s.split("T", 1)[0]
    if len(s) < 10:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return s[:10]

def _clip_warn(value: str, max_len: int, label: str, warnings: list[str]) -> str:
    s = str(value or "")
    if len(s) > max_len:
        warnings.append(f"{label} length {len(s)} exceeds {max_len}; RM may silently truncate")
        return s[:max_len]
    return s

def _location_headers(location_id: int | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if location_id is not None:
        headers["x-rm12api-locationid"] = str(int(location_id))
    return headers

def normalize_bill_detail_line(line: dict[str, Any], *, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(line, dict):
        raise ValueError("each BillDetails line must be an object")
    prop = line.get("PropertyID") if line.get("PropertyID") is not None else line.get("property_id")
    gl = line.get("GLAccountID") if line.get("GLAccountID") is not None else line.get("gl_account_id")
    amt = line.get("Amount") if line.get("Amount") is not None else line.get("amount")
    out: dict[str, Any] = {
        "PropertyID": _as_positive_int(prop, "PropertyID"),
        "GLAccountID": _as_positive_int(gl, "GLAccountID"),
        "Amount": _as_money(amt, "Amount"),
    }
    unit = line.get("UnitID") if line.get("UnitID") is not None else line.get("unit_id")
    if unit is not None and int(unit) > 0:
        out["UnitID"] = int(unit)
    comment = line.get("Comment") if line.get("Comment") is not None else line.get("comment")
    if comment is not None and str(comment).strip():
        out["Comment"] = _clip_warn(str(comment).strip(), 255, "BillDetails.Comment", warnings)
    return out

def build_vendor_bill_body(
    *,
    account_id: int,
    transaction_date: str,
    post_date: str,
    due_date: str,
    bill_details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    account_type: str = "Vendor",
    file_attachments: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build one Bill model dict + validation warnings."""
    warnings: list[str] = []
    if not bill_details:
        raise ValueError("bill_details is required (one or more lines)")
    details = [normalize_bill_detail_line(x, warnings=warnings) for x in bill_details]
    total = round(sum(float(d["Amount"]) for d in details), 2)
    if total <= 0:
        raise ValueError("bill total from detail lines must be greater than 0")
    body: dict[str, Any] = {
        "AccountID": _as_positive_int(account_id, "AccountID"),
        "AccountType": (account_type or "Vendor").strip() or "Vendor",
        "TransactionDate": _as_date_str(transaction_date, "TransactionDate"),
        "PostDate": _as_date_str(post_date, "PostDate"),
        "DueDate": _as_date_str(due_date, "DueDate"),
        "BillDetails": details,
    }
    if reference is not None and str(reference).strip():
        body["Reference"] = _clip_warn(str(reference).strip(), 50, "Reference", warnings)
    if comment is not None and str(comment).strip():
        body["Comment"] = _clip_warn(str(comment).strip(), 255, "Comment", warnings)
    if file_attachments:
        fa: list[dict[str, Any]] = []
        for item in file_attachments:
            if not isinstance(item, dict):
                raise ValueError("file_attachments entries must be objects")
            tag = str(item.get("MetaTag") or item.get("meta_tag") or "").strip()
            if not tag:
                raise ValueError("FileAttachments.MetaTag is required")
            if not tag.replace("_", "").isalnum():
                raise ValueError(f"MetaTag must be alphanumeric/underscore: {tag}")
            entry: dict[str, Any] = {"MetaTag": tag}
            desc = item.get("Description") if item.get("Description") is not None else item.get("description")
            if desc is not None and str(desc).strip():
                entry["Description"] = str(desc).strip()
            fa.append(entry)
        body["FileAttachments"] = fa
    return body, warnings

def build_create_vendor_bill_plan(
    *,
    account_id: int,
    transaction_date: str,
    post_date: str,
    due_date: str,
    bill_details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    account_type: str = "Vendor",
    file_path: str | None = None,
    file_description: str = "",
    meta_tag: str = "File1",
    location_id: int | None = None,
    embeds: str = "BillDetails,FileAttachments",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create one vendor bill; optionally attach one PDF/file in the same multipart request."""
    warnings: list[str] = []
    file_attachments = None
    file_meta = None
    tag = (meta_tag or "File1").strip() or "File1"
    if file_path:
        file_meta = resolve_upload_file(file_path=file_path)
        file_attachments = [
            {
                "MetaTag": tag,
                "Description": (file_description or file_meta["filename"]).strip() or file_meta["filename"],
            }
        ]
    body, wmsgs = build_vendor_bill_body(
        account_id=account_id,
        transaction_date=transaction_date,
        post_date=post_date,
        due_date=due_date,
        bill_details=bill_details,
        reference=reference,
        comment=comment,
        account_type=account_type,
        file_attachments=file_attachments,
    )
    warnings.extend(wmsgs)
    headers = _location_headers(location_id)
    if file_path and file_meta:
        step = {
            "method": "POST",
            "path": "/Bills",
            "params": {"embeds": embeds},
            "multipart": {
                "data_fields": {"Body": json.dumps([body])},
                "file_fields": {
                    tag: {
                        "path": file_meta["path"],
                        "filename": file_meta["filename"],
                        "content_type": file_meta["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": file_meta,
            "notes": [
                "Create vendor bill + attachment in one multipart request",
                "text part name must be Body; file part name must equal MetaTag",
            ],
            "validation_warnings": warnings,
        }
        return [step], warnings
    step = {
        "method": "POST",
        "path": "/Bills",
        "params": {"embeds": "BillDetails"},
        "json_body": [body],
        "extra_headers": headers or None,
        "notes": [
            "Create vendor bill JSON array body",
            "Header Amount is calculated from BillDetails",
        ],
        "validation_warnings": warnings,
    }
    return [step], warnings

def build_attach_bill_file_plan(
    *,
    bill_id: int,
    file_path: str,
    description: str = "",
    mode: str = "upload",
    meta_tag: str = "File1",
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """Attach a file to an existing bill."""
    bid = _as_positive_int(bill_id, "bill_id")
    info = resolve_upload_file(file_path=file_path)
    headers = _location_headers(location_id)
    mode_l = (mode or "upload").strip().lower()
    if mode_l in {"upload", "uploadattachment", "stream"}:
        return [
            {
                "method": "POST",
                "path": f"/Bills/{bid}/UploadAttachment",
                "multipart": {
                    "data_fields": {},
                    "file_fields": {
                        "file": {
                            "path": info["path"],
                            "filename": info["filename"],
                            "content_type": info["content_type"],
                        }
                    },
                },
                "extra_headers": headers or None,
                "file_meta": info,
                "notes": ["Single-file stream attach; part name must be file"],
            }
        ]
    if mode_l in {"file_attachments", "fileattachments", "multi"}:
        tag = (meta_tag or "File1").strip() or "File1"
        if not tag.replace("_", "").isalnum():
            raise ValueError("meta_tag must be alphanumeric/underscore")
        payload = [
            {
                "MetaTag": tag,
                "Description": (description or info["filename"]).strip() or info["filename"],
            }
        ]
        return [
            {
                "method": "POST",
                "path": f"/Bills/{bid}/FileAttachments",
                "multipart": {
                    "data_fields": {"Payload": json.dumps(payload)},
                    "file_fields": {
                        tag: {
                            "path": info["path"],
                            "filename": info["filename"],
                            "content_type": info["content_type"],
                        }
                    },
                },
                "extra_headers": headers or None,
                "file_meta": info,
                "notes": [
                    "Multi attach to existing bill; text part name is Payload",
                    "file part name must equal MetaTag",
                ],
            }
        ]
    raise ValueError("mode must be upload or file_attachments")

def build_update_vendor_bill_plan(
    *,
    bill_id: int,
    fields: dict[str, Any],
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """POST upsert update with fields= mask. Only listed properties are written."""
    bid = _as_positive_int(bill_id, "bill_id")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty object of properties to change")
    allowed = {
        "DueDate", "PostDate", "TransactionDate", "Reference",
        "Comment", "BankID", "PayMethod", "IsApproved",
    }
    body: dict[str, Any] = {"ID": bid}
    field_names = ["ID"]
    for k, v in fields.items():
        key = str(k)
        alias = {
            "due_date": "DueDate",
            "post_date": "PostDate",
            "transaction_date": "TransactionDate",
            "reference": "Reference",
            "comment": "Comment",
            "bank_id": "BankID",
            "pay_method": "PayMethod",
            "is_approved": "IsApproved",
        }.get(key, key)
        if alias not in allowed:
            raise ValueError(f"update field not allowlisted: {key}")
        if alias in {"DueDate", "PostDate", "TransactionDate"} and v is not None:
            body[alias] = _as_date_str(v, alias)
        else:
            body[alias] = v
        if alias not in field_names:
            field_names.append(alias)
    headers = _location_headers(location_id)
    return [
        {
            "method": "POST",
            "path": "/Bills",
            "params": {"fields": ",".join(field_names)},
            "json_body": [body],
            "extra_headers": headers or None,
            "notes": [
                "POST upsert update; fields= mask required to avoid resetting omitted properties",
            ],
        }
    ]


# ---- Credit card charges (Alexander James, 2026-10-29) ----
# Not a vendor bill. Not a tenant rent receipt.
# Create+attach: POST /CreditCardTransactions?embeds=CreditCardTransactionDetails,Attachments
# Multipart text part name is lowercase `body` (not Bills' `Body`). File part name = MetaTag.
# Header Amount is ignored. Do not send TransactionType.
# Add to existing: POST /CreditCardTransactions/{ID}/Attachments, same multipart shape.
# Update: POST /CreditCardTransactions?fields=ID,... with ID in the body. No PUT/PATCH.
# embeds=Attachments is required or the file is silently dropped.

def normalize_cc_detail_line(line: dict[str, Any], *, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(line, dict):
        raise ValueError("each CreditCardTransactionDetails line must be an object")
    prop = line.get("PropertyID") if line.get("PropertyID") is not None else line.get("property_id")
    gl = line.get("GLAccountID") if line.get("GLAccountID") is not None else line.get("gl_account_id")
    amt = line.get("Amount") if line.get("Amount") is not None else line.get("amount")
    out: dict[str, Any] = {
        "PropertyID": _as_positive_int(prop, "PropertyID"),
        "GLAccountID": _as_positive_int(gl, "GLAccountID"),
        "Amount": _as_money(amt, "Amount"),
    }
    unit = line.get("UnitID") if line.get("UnitID") is not None else line.get("unit_id")
    if unit is not None and str(unit).strip() and int(unit) > 0:
        out["UnitID"] = int(unit)
    comment = line.get("Comment") if line.get("Comment") is not None else line.get("comment")
    if comment is not None and str(comment).strip():
        out["Comment"] = _clip_warn(str(comment).strip(), 255, "CreditCardTransactionDetails.Comment", warnings)
    return out

def build_credit_card_transaction_body(
    *,
    credit_card_id: int,
    account_id: int,
    transaction_date: str,
    details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build one CreditCardTransaction model. Header Amount is omitted on purpose."""
    warnings: list[str] = []
    if not details:
        raise ValueError("details is required (one or more lines)")
    lines = [normalize_cc_detail_line(x, warnings=warnings) for x in details]
    total = round(sum(float(d["Amount"]) for d in lines), 2)
    if total <= 0:
        raise ValueError("credit card total from detail lines must be greater than 0")
    body: dict[str, Any] = {
        "CreditCardID": _as_positive_int(credit_card_id, "CreditCardID"),
        "AccountID": _as_positive_int(account_id, "AccountID"),
        "AccountType": "Vendor",
        "TransactionDate": _as_date_str(transaction_date, "TransactionDate"),
        "CreditCardTransactionDetails": lines,
    }
    if reference is not None and str(reference).strip():
        body["Reference"] = _clip_warn(str(reference).strip(), 50, "Reference", warnings)
    if comment is not None and str(comment).strip():
        body["Comment"] = _clip_warn(str(comment).strip(), 255, "Comment", warnings)
    if attachments:
        atts: list[dict[str, Any]] = []
        for item in attachments:
            if not isinstance(item, dict):
                raise ValueError("attachments entries must be objects")
            tag = str(item.get("MetaTag") or item.get("meta_tag") or "").strip()
            if not tag:
                raise ValueError("Attachments.MetaTag is required")
            if not tag.replace("_", "").isalnum():
                raise ValueError(f"MetaTag must be alphanumeric/underscore: {tag}")
            entry: dict[str, Any] = {"MetaTag": tag}
            desc = item.get("Description") if item.get("Description") is not None else item.get("description")
            if desc is not None and str(desc).strip():
                entry["Description"] = str(desc).strip()
            atts.append(entry)
        body["Attachments"] = atts
    warnings.append("header Amount omitted; RM totals the detail lines")
    warnings.append("TransactionDate on or before a property hard close will be rejected")
    return body, warnings

def build_create_credit_card_transaction_plan(
    *,
    credit_card_id: int,
    account_id: int,
    transaction_date: str,
    details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    file_path: str | None = None,
    file_description: str = "",
    meta_tag: str = "file1",
    location_id: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create one credit card charge. With a file, multipart part name is `body`."""
    warnings: list[str] = []
    attachments = None
    file_meta = None
    tag = (meta_tag or "file1").strip() or "file1"
    if file_path:
        file_meta = resolve_upload_file(file_path=file_path)
        attachments = [
            {
                "MetaTag": tag,
                "Description": (file_description or file_meta["filename"]).strip() or file_meta["filename"],
            }
        ]
    body, wmsgs = build_credit_card_transaction_body(
        credit_card_id=credit_card_id,
        account_id=account_id,
        transaction_date=transaction_date,
        details=details,
        reference=reference,
        comment=comment,
        attachments=attachments,
    )
    warnings.extend(wmsgs)
    headers = _location_headers(location_id)
    if file_path and file_meta:
        step = {
            "method": "POST",
            "path": "/CreditCardTransactions",
            "params": {"embeds": "CreditCardTransactionDetails,Attachments"},
            "multipart": {
                "data_fields": {"body": json.dumps([body])},
                "file_fields": {
                    tag: {
                        "path": file_meta["path"],
                        "filename": file_meta["filename"],
                        "content_type": file_meta["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": file_meta,
            "notes": [
                "Create credit card charge + scan in one multipart request",
                "text part name must be lowercase body; file part name must equal MetaTag",
                "embeds=Attachments is required or the file is silently dropped",
                "not a vendor bill; not a tenant rent receipt",
            ],
            "validation_warnings": warnings,
        }
        return [step], warnings
    step = {
        "method": "POST",
        "path": "/CreditCardTransactions",
        "params": {"embeds": "CreditCardTransactionDetails"},
        "json_body": [body],
        "extra_headers": headers or None,
        "notes": [
            "Create credit card charge JSON array body",
            "header Amount omitted; total is the sum of detail lines",
            "not a vendor bill; not a tenant rent receipt",
        ],
        "validation_warnings": warnings,
    }
    return [step], warnings

def build_attach_credit_card_file_plan(
    *,
    transaction_id: int,
    file_path: str,
    description: str = "",
    meta_tag: str = "file1",
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """Attach a scan to an existing credit card charge."""
    tid = _as_positive_int(transaction_id, "transaction_id")
    info = resolve_upload_file(file_path=file_path)
    tag = (meta_tag or "file1").strip() or "file1"
    if not tag.replace("_", "").isalnum():
        raise ValueError("meta_tag must be alphanumeric/underscore")
    payload = [
        {
            "MetaTag": tag,
            "Description": (description or info["filename"]).strip() or info["filename"],
        }
    ]
    headers = _location_headers(location_id)
    return [
        {
            "method": "POST",
            "path": f"/CreditCardTransactions/{tid}/Attachments",
            "multipart": {
                "data_fields": {"body": json.dumps(payload)},
                "file_fields": {
                    tag: {
                        "path": info["path"],
                        "filename": info["filename"],
                        "content_type": info["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": info,
            "notes": [
                "Add attachment to existing credit card transaction",
                "text part name is lowercase body; file part name must equal MetaTag",
            ],
        }
    ]

def build_update_credit_card_transaction_plan(
    *,
    transaction_id: int,
    fields: dict[str, Any],
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """POST upsert with fields= mask. Detail lines and amount stay put unless a later tool allows them."""
    tid = _as_positive_int(transaction_id, "transaction_id")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty object of properties to change")
    allowed = {"Comment", "Reference", "TransactionDate"}
    alias = {
        "comment": "Comment",
        "reference": "Reference",
        "transaction_date": "TransactionDate",
    }
    body: dict[str, Any] = {"ID": tid}
    field_names = ["ID"]
    for k, v in fields.items():
        key = alias.get(str(k), str(k))
        if key not in allowed:
            raise ValueError(
                f"update field not allowlisted: {k}. v1 allows Comment, Reference, TransactionDate only"
            )
        if key == "TransactionDate":
            body[key] = _as_date_str(v, key)
        elif key == "Reference":
            body[key] = _clip_warn(str(v or "").strip(), 50, "Reference", [])
        else:
            body[key] = _clip_warn(str(v or "").strip(), 255, "Comment", [])
        if key not in field_names:
            field_names.append(key)
    headers = _location_headers(location_id)
    return [
        {
            "method": "POST",
            "path": "/CreditCardTransactions",
            "params": {"fields": ",".join(field_names)},
            "json_body": [body],
            "extra_headers": headers or None,
            "notes": [
                "POST upsert update; fields= mask required or a partial body fails validation",
                "detail lines, amount, and attachments are not in this mask",
            ],
        }
    ]


# ---- Paid bank receipt as a Check (Alexander James, 2026-10-29) ----
# Not a vendor bill. Not a tenant rent receipt. Not a history note.
# Create+attach: POST /Checks?embeds=CheckDetails,FileAttachments
# Multipart text part name is lowercase `body`. File part name = MetaTag (example File1).
# Header Amount is ignored. Payee is not filled from the vendor.
# Non-default bank: saveOptions=IgnorePropDefaultBankWarning,true plus the privilege.
# Add to existing: POST /Checks/{ID}/FileAttachments. UploadAttachment is the no-JSON alternate; v1 uses FileAttachments.
# Update: POST /Checks?fields=ID,... No PUT/PATCH. v1 does not rewrite CheckDetails.

def normalize_check_detail_line(line: dict[str, Any], *, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(line, dict):
        raise ValueError("each CheckDetails line must be an object")
    prop = line.get("PropertyID") if line.get("PropertyID") is not None else line.get("property_id")
    gl = line.get("GLAccountID") if line.get("GLAccountID") is not None else line.get("gl_account_id")
    amt = line.get("Amount") if line.get("Amount") is not None else line.get("amount")
    out: dict[str, Any] = {
        "PropertyID": _as_positive_int(prop, "PropertyID"),
        "GLAccountID": _as_positive_int(gl, "GLAccountID"),
        "Amount": _as_money(amt, "Amount"),
    }
    unit = line.get("UnitID") if line.get("UnitID") is not None else line.get("unit_id")
    if unit is not None and str(unit).strip() and int(unit) > 0:
        out["UnitID"] = int(unit)
    comment = line.get("Comment") if line.get("Comment") is not None else line.get("comment")
    if comment is not None and str(comment).strip():
        out["Comment"] = _clip_warn(str(comment).strip(), 255, "CheckDetails.Comment", warnings)
    return out


def build_check_body(
    *,
    bank_id: int,
    account_id: int,
    transaction_date: str,
    details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    payee: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build one Check model. Header Amount is omitted on purpose."""
    warnings: list[str] = []
    if not details:
        raise ValueError("details is required (one or more lines)")
    lines = [normalize_check_detail_line(x, warnings=warnings) for x in details]
    total = round(sum(float(d["Amount"]) for d in lines), 2)
    if total <= 0:
        raise ValueError("check total from detail lines must be greater than 0")
    body: dict[str, Any] = {
        "BankID": _as_positive_int(bank_id, "BankID"),
        "AccountID": _as_positive_int(account_id, "AccountID"),
        "AccountType": "Vendor",
        "TransactionDate": _as_date_str(transaction_date, "TransactionDate"),
        "CheckDetails": lines,
    }
    if reference is not None and str(reference).strip():
        body["Reference"] = _clip_warn(str(reference).strip(), 25, "Reference", warnings)
    if comment is not None and str(comment).strip():
        body["Comment"] = _clip_warn(str(comment).strip(), 255, "Comment", warnings)
    if payee is not None and str(payee).strip():
        body["Payee"] = _clip_warn(str(payee).strip(), 255, "Payee", warnings)
    else:
        warnings.append("Payee omitted; RM does not fill the check name from the vendor")
    if attachments:
        atts: list[dict[str, Any]] = []
        for item in attachments:
            if not isinstance(item, dict):
                raise ValueError("attachments entries must be objects")
            tag = str(item.get("MetaTag") or item.get("meta_tag") or "").strip()
            if not tag:
                raise ValueError("FileAttachments.MetaTag is required")
            if not tag.replace("_", "").isalnum():
                raise ValueError(f"MetaTag must be alphanumeric/underscore: {tag}")
            entry: dict[str, Any] = {"MetaTag": tag}
            desc = item.get("Description") if item.get("Description") is not None else item.get("description")
            if desc is not None and str(desc).strip():
                entry["Description"] = str(desc).strip()
            atts.append(entry)
        body["FileAttachments"] = atts
    warnings.append("header Amount omitted; RM totals the detail lines and ignores a header Amount")
    warnings.append("not a tenant rent receipt; not a vendor bill; scan rides the check, not a history note")
    warnings.append("TransactionDate on or before a property hard close will be rejected")
    return body, warnings


def build_create_check_plan(
    *,
    bank_id: int,
    account_id: int,
    transaction_date: str,
    details: list[dict[str, Any]],
    reference: str = "",
    comment: str = "",
    payee: str = "",
    file_path: str | None = None,
    file_description: str = "",
    meta_tag: str = "File1",
    ignore_prop_default_bank_warning: bool = False,
    location_id: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create one paid-receipt check. With a file, multipart part name is `body`."""
    warnings: list[str] = []
    attachments = None
    file_meta = None
    tag = (meta_tag or "File1").strip() or "File1"
    if file_path:
        file_meta = resolve_upload_file(file_path=file_path)
        attachments = [
            {
                "MetaTag": tag,
                "Description": (file_description or file_meta["filename"]).strip() or file_meta["filename"],
            }
        ]
    body, wmsgs = build_check_body(
        bank_id=bank_id,
        account_id=account_id,
        transaction_date=transaction_date,
        details=details,
        reference=reference,
        comment=comment,
        payee=payee,
        attachments=attachments,
    )
    warnings.extend(wmsgs)
    params: dict[str, Any] = {
        "embeds": "CheckDetails,FileAttachments" if file_path else "CheckDetails",
    }
    if ignore_prop_default_bank_warning:
        params["saveOptions"] = "IgnorePropDefaultBankWarning,true"
        warnings.append(
            "non-default bank: saveOptions=IgnorePropDefaultBankWarning,true; needs Write checks from banks that are not default"
        )
    else:
        warnings.append("BankID should be each line property's DefaultBankID unless the warning saveOption is set")
    headers = _location_headers(location_id)
    if file_path and file_meta:
        step = {
            "method": "POST",
            "path": "/Checks",
            "params": params,
            "multipart": {
                "data_fields": {"body": json.dumps([body])},
                "file_fields": {
                    tag: {
                        "path": file_meta["path"],
                        "filename": file_meta["filename"],
                        "content_type": file_meta["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": file_meta,
            "notes": [
                "Create check + scan in one multipart request",
                "text part name must be lowercase body; file part name must equal MetaTag",
                "embeds=FileAttachments is required or the file is silently dropped",
                "not a vendor bill; not a tenant rent receipt; not a history note",
            ],
            "validation_warnings": warnings,
        }
        return [step], warnings
    step = {
        "method": "POST",
        "path": "/Checks",
        "params": params,
        "json_body": [body],
        "extra_headers": headers or None,
        "notes": [
            "Create check JSON array body",
            "header Amount omitted; total is the sum of detail lines",
            "not a vendor bill; not a tenant rent receipt",
        ],
        "validation_warnings": warnings,
    }
    return [step], warnings


def build_attach_check_file_plan(
    *,
    check_id: int,
    file_path: str,
    description: str = "",
    meta_tag: str = "File1",
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """Attach a scan to an existing check via FileAttachments."""
    cid = _as_positive_int(check_id, "check_id")
    info = resolve_upload_file(file_path=file_path)
    tag = (meta_tag or "File1").strip() or "File1"
    if not tag.replace("_", "").isalnum():
        raise ValueError("meta_tag must be alphanumeric/underscore")
    payload = [
        {
            "MetaTag": tag,
            "Description": (description or info["filename"]).strip() or info["filename"],
        }
    ]
    headers = _location_headers(location_id)
    return [
        {
            "method": "POST",
            "path": f"/Checks/{cid}/FileAttachments",
            "multipart": {
                "data_fields": {"body": json.dumps(payload)},
                "file_fields": {
                    tag: {
                        "path": info["path"],
                        "filename": info["filename"],
                        "content_type": info["content_type"],
                    }
                },
            },
            "extra_headers": headers or None,
            "file_meta": info,
            "notes": [
                "Add attachment to existing check",
                "text part name is lowercase body; file part name must equal MetaTag",
                "UploadAttachment is the no-JSON alternate and is not used here",
            ],
        }
    ]


def build_update_check_plan(
    *,
    check_id: int,
    fields: dict[str, Any],
    location_id: int | None = None,
) -> list[dict[str, Any]]:
    """POST upsert with fields= mask. Detail lines and amount stay put."""
    cid = _as_positive_int(check_id, "check_id")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty object of properties to change")
    allowed = {"Comment", "Reference", "Payee", "TransactionDate"}
    alias = {
        "comment": "Comment",
        "reference": "Reference",
        "payee": "Payee",
        "transaction_date": "TransactionDate",
    }
    body: dict[str, Any] = {"ID": cid}
    field_names = ["ID"]
    for k, v in fields.items():
        key = alias.get(str(k), str(k))
        if key not in allowed:
            raise ValueError(
                f"update field not allowlisted: {k}. v1 allows Comment, Reference, Payee, TransactionDate only"
            )
        if key == "TransactionDate":
            body[key] = _as_date_str(v, key)
        elif key == "Reference":
            body[key] = _clip_warn(str(v or "").strip(), 25, "Reference", [])
        else:
            body[key] = _clip_warn(str(v or "").strip(), 255, key, [])
        if key not in field_names:
            field_names.append(key)
    headers = _location_headers(location_id)
    return [
        {
            "method": "POST",
            "path": "/Checks",
            "params": {"fields": ",".join(field_names)},
            "json_body": [body],
            "extra_headers": headers or None,
            "notes": [
                "POST upsert update; fields= mask required or omitted fields are reset",
                "CheckDetails, amount, BankID, and AccountID are not in this mask",
            ],
        }
    ]


def build_attach_vendor_file_plan(
    *,
    vendor_id: int,
    file_path: str,
    note: str,
    history_category_id: int | None = None,
    history_date: str | None = None,
    location_id: int | None = None,
    mode: str = "two_step",
    meta_tag: str = "file1",
) -> list[dict[str, Any]]:
    """Attach a PDF/file to a vendor via HistoryNotes (no direct vendor docs API).

    RM support 2026-09-27:
      Option A (one_request): multipart POST /Vendors/{id}/HistoryNotes?embeds=...
        text part `body` = note JSON with HistoryAttachments MetaTag
        file part named to match MetaTag (default file1)
      Option B (two_step): POST note JSON, then POST /HistoryNotes/{id}/UploadAttachment
        (singular UploadAttachment already live-verified on WO path)

    Privilege min: History > Vendor History/Notes > Add
    POST /Vendors/{id}/History remains 405 — use HistoryNotes.
    """
    vid = int(vendor_id)
    if vid <= 0:
        raise ValueError("vendor_id must be positive")
    info = resolve_upload_file(file_path=file_path)
    text_note = (note or "").strip() or f"Vendor file attachment: {info['filename']}"
    cat = int(history_category_id) if history_category_id is not None else None
    hdate = (history_date or "").strip() or None
    tag = (meta_tag or "file1").strip() or "file1"
    mode_l = (mode or "two_step").strip().lower()
    headers: dict[str, str] = {}
    if location_id is not None:
        headers["X-RM12Api-LocationID"] = str(int(location_id))

    note_body: dict[str, Any] = {"Note": text_note}
    if cat is not None:
        note_body["HistoryCategoryID"] = cat
    if hdate:
        note_body["HistoryDate"] = hdate

    if mode_l in {"one_request", "one-shot", "oneshot", "multipart", "a", "option_a"}:
        body = {
            **note_body,
            "HistoryAttachments": [{"File": {"MetaTag": tag}}],
        }
        return [
            {
                "method": "POST",
                "path": (
                    f"/Vendors/{vid}/HistoryNotes"
                    "?embeds=HistoryAttachments,HistoryAttachments.File"
                ),
                "multipart": {
                    "data_fields": {
                        # Support sample uses lowercase `body` for HistoryNotes multipart
                        "body": json.dumps([body]),
                    },
                    "file_fields": {
                        tag: {
                            "path": info["path"],
                            "filename": info["filename"],
                            "content_type": info["content_type"],
                        }
                    },
                },
                "extra_headers": headers or None,
                "file_meta": info,
                "notes": [
                    "Vendor file attach Option A — one multipart HistoryNotes create",
                    "No direct vendor documents resource; files ride HistoryNotes",
                ],
            }
        ]

    # Default Option B: create note, then singular UploadAttachment (proven path)
    step1 = {
        "method": "POST",
        "path": f"/Vendors/{vid}/HistoryNotes",
        "json_body": note_body,
        "extra_headers": headers or None,
        "notes": ["Vendor HistoryNotes create (Option B step 1)"],
    }
    step2 = {
        "method": "POST",
        "path": "/HistoryNotes/{history_id}/UploadAttachment",
        "multipart": {
            "data_fields": {
                "File": json.dumps({"HistoryID": "{history_id}"}),
            },
            "file_fields": {
                "Attachment": {
                    "path": info["path"],
                    "filename": info["filename"],
                    "content_type": info["content_type"],
                }
            },
        },
        "extra_headers": headers or None,
        "file_meta": info,
        "notes": [
            "Vendor HistoryNotes UploadAttachment (Option B step 2)",
            "Singular UploadAttachment already LIVE-verified on WO path",
        ],
        "needs_history_id_from_prior_step": True,
    }
    return [step1, step2]


def guarded_write(
    client: RMClient,
    *,
    action: str,
    dry_run: bool,
    idempotency_key: str,
    reason: str,
    approved_by: str | None,
    request_plan: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit_id = str(uuid.uuid4())
    base_event = {
        "audit_id": audit_id,
        "action": action,
        "dry_run": dry_run,
        "idempotency_key": idempotency_key,
        "reason": reason,
        "approved_by": approved_by,
        "meta": meta or {},
        "request_plan": request_plan,
    }
    if dry_run:
        _audit({**base_event, "result": "dry_run"})
        return {
            "ok": True,
            "dry_run": True,
            "would_send": request_plan,
            "sent": None,
            "entity_ids": {},
            "audit_id": audit_id,
            "permission_note": (load_write_matrix().get("summary") or {}),
        }
    if not (approved_by or "").strip():
        _audit({**base_event, "result": "rejected_missing_approval"})
        return {
            "ok": False,
            "dry_run": False,
            "error": {
                "code": "approval_required",
                "message": "Live writes require approved_by when dry_run=false",
            },
            "would_send": request_plan,
            "audit_id": audit_id,
        }

    errors: list[dict[str, Any]] = []
    sent_steps: list[dict[str, Any]] = []
    entity_ids: dict[str, Any] = {}
    rank = {
        "invalid_payload": 0,
        "write_not_permitted": 1,
        "method_not_allowed": 2,
        "auth_failed": 3,
        "endpoint_not_found": 4,
        "write_failed": 5,
    }

    # Multi-step plans (e.g. link unit+property+tenant, WO photo note+attach)
    # must run every step. Single-step plans behave as before.
    for step in request_plan:
        method = step["method"]
        path = step["path"]
        body = step.get("json_body")
        multipart = step.get("multipart")
        extra_headers = step.get("extra_headers") or None

        # Stitch HistoryID from prior step into templated upload paths.
        if step.get("needs_history_id_from_prior_step"):
            hid = entity_ids.get("HistoryID")
            if hid is None:
                err = {
                    "code": "invalid_payload",
                    "message": "prior step did not return HistoryID; cannot attach file",
                    "path": path,
                    "method": method,
                }
                errors.append(err)
                _audit({**base_event, "result": "attempt_failed", "path": path, "error": err})
                break
            path = path.replace("{history_id}", str(hid))
            if multipart and isinstance(multipart.get("data_fields"), dict):
                fixed = {}
                for k, v in multipart["data_fields"].items():
                    if isinstance(v, str):
                        fixed[k] = v.replace("{history_id}", str(hid))
                    else:
                        fixed[k] = v
                multipart = {**multipart, "data_fields": fixed}

        try:
            open_handles: list[Any] = []
            try:
                if multipart:
                    data_fields = dict(multipart.get("data_fields") or {})
                    file_fields = multipart.get("file_fields") or {}
                    files_arg: dict[str, Any] = {}
                    for field_name, fmeta in file_fields.items():
                        fpath = Path(fmeta["path"])
                        fh = fpath.open("rb")
                        open_handles.append(fh)
                        files_arg[field_name] = (
                            fmeta.get("filename") or fpath.name,
                            fh,
                            fmeta.get("content_type") or "application/octet-stream",
                        )
                    r = client.request(
                        method,
                        path,
                        params=step.get("params"),
                        data=data_fields,
                        files=files_arg,
                        extra_headers=extra_headers,
                        timeout=120,
                    )
                else:
                    r = client.request(
                        method,
                        path,
                        params=step.get("params"),
                        json_body=body,
                        extra_headers=extra_headers,
                    )
            finally:
                for fh in open_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass

            payload = None
            if r.content:
                try:
                    payload = r.json()
                except Exception:
                    payload = (r.text or "")[:500]
            step_ids = extract_entity_ids(payload)
            entity_ids.update(step_ids)
            sent_step = {
                "method": method,
                "path": path,
                "status": r.status_code,
                "response": payload,
                "multipart": bool(multipart),
            }
            sent_steps.append(sent_step)
            _audit(
                {
                    **base_event,
                    "result": "sent",
                    "status": r.status_code,
                    "response": payload,
                    "path": path,
                    "entity_ids": step_ids,
                }
            )
        except Exception as exc:  # noqa: BLE001
            err = classify_write_error(exc)
            err["path"] = path
            err["method"] = method
            errors.append(err)
            _audit({**base_event, "result": "attempt_failed", "path": path, "error": err})
            # For multi-step link plans, stop on first failure after any successes.
            if sent_steps:
                break
            continue

    if sent_steps and not errors:
        sent_out: dict[str, Any] | list[dict[str, Any]]
        if len(sent_steps) == 1:
            sent_out = sent_steps[0]
        else:
            sent_out = sent_steps
        return {
            "ok": True,
            "dry_run": False,
            "would_send": request_plan,
            "sent": sent_out,
            "entity_ids": entity_ids,
            "audit_id": audit_id,
            "steps_ok": len(sent_steps),
        }

    if sent_steps and errors:
        # Partial success (some links applied before a failure)
        _audit({**base_event, "result": "partial", "sent_steps": sent_steps, "error": errors[0]})
        return {
            "ok": False,
            "dry_run": False,
            "error": {
                **errors[0],
                "code": errors[0].get("code") or "write_failed",
                "message": f"partial write: {len(sent_steps)} step(s) ok, then failed",
                "attempts": errors,
            },
            "would_send": request_plan,
            "sent": sent_steps,
            "entity_ids": entity_ids,
            "audit_id": audit_id,
            "steps_ok": len(sent_steps),
            "permission_note": (load_write_matrix().get("summary") or {}),
        }

    last_err: dict[str, Any] | None = None
    if errors:
        last_err = sorted(errors, key=lambda e: rank.get(str(e.get("code")), 99))[0]
        last_err = {**last_err, "attempts": errors}
    _audit({**base_event, "result": "failed", "error": last_err})
    return {
        "ok": False,
        "dry_run": False,
        "error": last_err or {"code": "write_failed", "message": "unknown write failure"},
        "would_send": request_plan,
        "sent": None,
        "entity_ids": {},
        "audit_id": audit_id,
        "permission_note": (load_write_matrix().get("summary") or {}),
    }
