# rm-mcp

Rent Manager MCP server for Kline Properties agents.

## Status
**Phase A + B + unlocks + uploads** (2026-09-16).
- Phase A: read tools live; compound boards/joins in Python
- Phase B: guarded write tools hardened + live-verified on Lucy (`dry_run=true` default; `approved_by` for live)
- Unlocks: WO update via collection POST upsert; Tenant/Unit/Property history via `/HistoryNotes`
Registered in OpenClaw as `rent-manager`.

## Design rules
- **Lazy auth** + shared on-disk token cache (about 10 logins per day; a cached token is reused until Rent Manager rejects it)
- **Coarse RM filters + Python join/filter/sort** (avoid clever fragile RM query syntax)
- **Normalize** all outputs to stable DTOs
- Account supports guarded writes (create WO/task, update WO via upsert, UDF, history notes). Some methods still 405 (PATCH/PUT WO, entity `/History` POST).

## Tools

### Auth / meta
- `rm_auth_status` — cached token state (does not log in)
- `rm_describe_resource` — local catalog resource notes
- `rm_whoami_capabilities` — probe which read endpoints respond (cached token only; does not log in)
- `rm_list_udfs` — UDF field definitions (Python-filtered)
- `rm_list_notes` — `/Notes` (warning: tiny global set, not full history)

### Properties / units / occupancy
- `rm_find_property` — name search
- `rm_get_property` — by PropertyID
- `rm_find_unit` — name/property/unit_id lookup
- `rm_unit_detail` — one unit + UDFs + address
- `rm_unit_udfs` — unit UDF pack (731/732/710 + all)
- `rm_list_for_lease` — UDF 731=Yes units
- `rm_occupancy` — occupied/vacant summary
- `rm_vacancy_board` — vacant + list-for-lease board with completeness flags

### Tenants / leases
- `rm_current_tenants` — current tenants for a property
- `rm_find_tenant` — name/email/phone/status/property
- `rm_tenant_detail` — tenant + contacts + leases
- `rm_lease_detail` — one lease
- `rm_list_leases` — filtered lease list (active_only in Python)

### Leasing / prospects
- `rm_list_prospects` — prospects by property/name

### Maintenance
- `rm_list_work_orders` — ServiceManagerIssues (open_only, embeds + Python filters)
- `rm_work_order_detail` — one WO
- `rm_update_work_order` — update existing WO via collection POST upsert (**dry_run default**)
- `rm_link_work_order` — link unit/property/tenant onto existing WO (**dry_run default**; append)
- `rm_list_vendors` — vendor directory

### Money (read-only)
- `rm_list_open_charges` — IsFullyAllocated=false (AccountID=TenantID)
- `rm_tenant_balance` — balance from GET /Tenants/{id}/Balance (a plain number; includes prepayments and credits); open-charge lines from OpenReceivables
- `rm_tenant_open_receivables` — GET /Tenants/{id}/OpenReceivables only. Line `balance` is AmountDue. Does not replace `rm_tenant_balance`
- `rm_list_recurring_charges` — GET /Tenants/{id}/RecurringCharges or GET /Units/{id}/RecurringCharges (`embeds=ChargeType`). `current_only` uses the FromDate/ToDate window. `rent_class` is an exact match on charge type Name
- `rm_rent_roll` — one property: each unit, current tenant, rent buckets, and balance. Units and tenants are one collection read each, filtered by PropertyID only (`embeds=RecurringCharges,RecurringCharges.ChargeType`; tenants also `embeds=Leases`). Current status is applied in Python. Each row reports base rent (RC tenant portion + RC8 subsidy), surcharges (RAC, RWD), concessions (RCI), other recurring charges, and the total. Balance is one GET /Tenants/{id}/Balance per occupied tenant in the result (plain number). Set `include_balances=false` to skip those calls. Charge codes live in `RENT_CHARGE_CODES` at the top of `normalize.py`
- `rm_list_payments` — recent payments for tenant

### People / owners / contact history (read-only)
- `rm_search_people` — plain-text QuickSearch across tenants, prospects, owners, vendors, and contacts (`searchText`). `kinds` defaults to all. One GET per kind. Compact hits: kind, id, name, phone, email, status when the payload has them
- `rm_find_owner` — list owners, or find by name / owner id / status. GET /Owners `embeds=PhoneNumbers`
- `rm_owner_detail` — one owner with Ownerships, Addresses, PhoneNumbers, and GET /Owners/{id}/Balance (plain number)
- `rm_property_owners` — GET /Properties/{id}/CurrentOwners and GET /Properties/{id}/PrimaryOwner
- `rm_contact_history` — HistoryNotes, HistoryCalls, HistoryEmails, and HistoryVisits for a tenant, prospect, or owner. Pages are downloaded and sorted newest-first in Python (no `orderby`). Optional `types` filter. `rm_list_history` is unchanged and still reads the combined /History feed


### Phase B — guarded writes / history
- `rm_write_matrix_status` — live capability matrix
- `rm_list_history` — history for Tenant/Unit/Property/ServiceManagerIssue (read)
- `rm_add_note` — WO history, entity HistoryNotes, or bare Notes (**dry_run default**)
- `rm_update_unit_udf` — UDF 710/731/732 (**dry_run default**; Yes/No normalized)
- `rm_create_work_order` — create SM issue (**dry_run default**)
- `rm_update_work_order` — update existing WO (**dry_run default**; collection POST upsert)
- `rm_link_work_order` — link unit/property/tenant onto existing WO (**dry_run default**; append)
- `rm_create_task` — create Task via **Subject** (**dry_run default**)
- `rm_upload_unit_image` — unit gallery `UploadImage` multipart (**dry_run default**; **LIVE OK 2026-09-21**)
- `rm_upload_history_attachment` — `HistoryNotes/{id}/UploadAttachment` multipart (**dry_run default**; **LIVE OK 2026-09-21**)
- `rm_attach_work_order_photo` — WO note + history attachment two-step (**dry_run default**; **LIVE OK 2026-09-21**)

Write contract:
- `dry_run=true` by default
- live requires `approved_by`
- allowlisted UDF field IDs only
- audit log `/tmp/rm_mcp_write_audit.jsonl`
- known-good payloads only (Notes need `Name`; Tasks need `Subject`)
- Tenant/Unit/Property `/History` POST is 405 → use `/HistoryNotes`
- WO `PATCH`/`PUT` is 405 → update via `POST /ServiceManagerIssues` with `ServiceManagerIssueID` (merged snapshot)

Live-verified on Lucy: WO create/update/link, unit UDF, WO history notes, Tenant/Unit/Property HistoryNotes, Notes/Items, Tasks.

## Catalog
```
rm_mcp/catalog/
  resources.json      # known paths/embeds/notes
  filters.md          # allowlisted filters
  write_matrix.json   # live write capability matrix
```

## Run manually
```bash
cd ~/.openclaw/workspace/rm-mcp
.venv/bin/python -m rm_mcp.server
```

## OpenClaw registration
```bash
openclaw mcp add rent-manager \
  --command /home/skline/.openclaw/workspace/rm-mcp/.venv/bin/python \
  --arg -m --arg rm_mcp.server \
  --cwd /home/skline/.openclaw/workspace/rm-mcp \
  --no-probe
openclaw mcp doctor rent-manager --probe
```

## Credentials
In order:
1. `RM_API_BASE`, `RM_USERNAME`, `RM_PASSWORD` env
2. `~/.openclaw/workspace/config/rentmanager.json`
3. `~/.rentmanager-credentials`

Token cache: `/tmp/rm_mcp_token.json` (reused until Rent Manager rejects it; no client-side expiry)

## Important RM quirks encoded here
- Auth body uses `UserName`
- Header is `x-rm12api-apitoken`
- Lazy auth + shared token cache
- Use `embeds=`, never `$expand=`
- Do **not** pass `fields=` with embeds
- Never use `/Units/search` for filtered unit queries
- UDF 731 List for Lease, 732 Asking Rent, 710 Tenant Turner
- Active leases = no `MoveOutDate` and no `DepartureDate`
- Tenant charges/payments use `AccountID = TenantID`
- Open charges often omit `AmountAllocated` — treated as unallocated (full balance)
- Work orders: use embeds `Status,Units,Properties,Tenants,Vendor` (bare PropertyID often null)
- Notes endpoint is not a full history API on this account (global note list)
- Notes/Items POST requires `Name`
- Tasks POST requires `Subject` (not Title)
- Tenant/Unit/Property `.../History` POST returns 405; use `.../HistoryNotes`
- WO `PATCH`/`PUT /ServiceManagerIssues/{id}` returns 405; update via collection POST upsert
- WO upsert mutable fields: Title, Description, Resolution, CustomerDescription, NoteText, StatusID, AssignedToUserID
- WO close/`IsClosed` still unproven (API demands CloseDate; field name not yet confirmed)
- WO entity link: `POST .../LinkUnits|LinkProperties|LinkTenants` with raw int array (append-only)
- WO entity unlink: Help mentions Unlink* but routes 404 on this account
- File uploads are multipart (not JSON); client uses requests files= boundary
- Unit images: `POST /Units/{id}/UploadImage` with Payload MetaTag + file field
- WO photos: history note first, then `POST /HistoryNotes/{HistoryID}/UploadAttachment`
- Credit card charges (2026-10-29, not live-verified): `rm_list_credit_cards`, `rm_get_credit_card_transaction`, `rm_create_credit_card_transaction`, `rm_attach_credit_card_file`, `rm_update_credit_card_transaction`. Multipart text part is lowercase `body`. Header Amount is omitted. Not a vendor bill. Not a rent receipt. Dry-run default.
- Paid bank receipts (2026-10-29, not live-verified): a check on `/Checks`, not a rent receipt. `rm_list_banks`, `rm_get_check`, `rm_create_check`, `rm_attach_check_file`, `rm_update_check`. Multipart text part is lowercase `body`. File part name matches MetaTag (`File1`). `embeds=FileAttachments` or the scan is dropped. Header Amount omitted. Non-default bank needs `saveOptions=IgnorePropDefaultBankWarning,true`. Dry-run default.
- Upload tools implemented 2026-09-16 from RM support Postman — WO photo path **LIVE OK**; unit UploadImage **LIVE OK 2026-09-21**

## Layout
```
rm-mcp/
  pyproject.toml
  README.md
  rm_mcp/
    client.py
    normalize.py
    server.py
    catalog/
```

## Next
0. Reload MCP + **live-verify** `rm_attach_work_order_photo` on a throwaway WO, then unit UploadImage
1. Reload OpenClaw MCP `rent-manager` so agents see `rm_update_work_order` + HistoryNotes path
2. Prefer dry-run drafts in agents; live only with explicit `approved_by`
3. Keep money/lease/delete mutations denied
4. **Do NOT** build WO close tooling for agents (Steve 2026-09-21: WOs billable; premature close costs money). Optional only: unit-property-tenant link-on-update
5. Optional: agent v1 Ops Copilot + Leasing on expanded reads + guarded writes
