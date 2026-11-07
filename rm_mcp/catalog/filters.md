# RM filter allowlist (KP)

## Always safe
- `PropertyID,eq,{id}`
- `UnitID,eq,{id}`
- `TenantID,eq,{id}`
- `LeaseID,eq,{id}`
- `Status,eq,Current|Past|Prospect`
- `IsActive,eq,true|false`
- `Name,eq,{exact}` / `Name,ct,{partial}`
- `AccountID,eq,{tenant_id}` for Charges/Payments
- `IsFullyAllocated,eq,false` for open charges
- `IsClosed,eq,false|true` for ServiceManagerIssues

## Proven UDF unit filter
```
UserDefinedValues.UserDefinedFieldID,eq,731;UserDefinedValues.Value,eq,Yes
```

## People, owners, history, charges
- QuickSearch is `searchText={plain text}` on `/Tenants/QuickSearch`, `/Prospects/QuickSearch`, `/Owners/QuickSearch`, `/Vendors/QuickSearch`, `/Contacts/QuickSearch`. It is not a `filters=` expression, so commas in the text stay.
- `OwnerID,eq,{id}` on `/Owners`
- `Name,ct,{text}` on `/Owners` (catalog lists EqualTo only; this server uses `ct`, same as tenants and vendors)
- `PropertyID,eq,{id}` alone on `/Units` and on `/Tenants` for the rent roll. Current vs past tenants is decided in Python.
- Owner lookup sends one clause only: `OwnerID,eq,{id}`, else `Name,ct,{text}`, else `Status,eq,{status}`. A second condition is applied in Python.
- Do not invent `PropertyID` on `/RecurringCharges` (the catalog marks it read-only with no filters). Use the tenant or unit subresource, or the `RecurringCharges` embed on the parent collection.
- History collections are `/Tenants|Prospects|Owners/{id}/HistoryNotes|HistoryCalls|HistoryEmails|HistoryVisits`. Sort them in Python. Do not send `orderby` on these reads.
- Recurring rent codes (exact Name match): RC, RC8, RAC, RWD, RCI. The list is `RENT_CHARGE_CODES` in `normalize.py`. GL 4311 and 4314 are the accounts those codes post to. Classification does not read the GL.
- Do not embed `RecurringCharges.ChargeType` on `/Tenants` or `/Units`. Those embed options include `RecurringCharges` and not the nested key; the call 404s with KeyNotFoundException. `embeds=ChargeType` is valid on `/Tenants/{id}/RecurringCharges` and `/Units/{id}/RecurringCharges`. Collection reads embed `RecurringCharges` only, then GET `/ChargeTypes` (no filter) and join on `ChargeTypeID`. A 403 on that catalog read is returned as an error. Do not fall back to one charge read per tenant.

## Prospects, renewals, agenda, reports
- `PropertyID,eq,{id}` alone on `/Prospects` and on `/Leases` for the expiring-lease list. Stage, status, lead source, name, and dates are applied in Python.
- `ProspectID,eq,{id}` alone on `/Prospects` for prospect detail.
- `LeaseID,eq,{id}` or `TenantID,eq,{id}` alone on `/Leases` for renewal detail. Do not send both.
- `/ProspectStages`, `/ProspectLostReasons`, `/LeadSources`, `/Appointments`, `/Tasks`, `/Users`, `/Reports`, and `/MemorizedReports` are unfiltered collection reads. Assigned user, due date, appointment date, and active flags are applied in Python.
- A staff member's tasks use GET `/Users/{id}/Tasks` (the id is in the path, not a second filter).
- Report parameters: GET `/Reports/{id}/ReportParameters` or GET `/MemorizedReports/{id}/RunParameters`. Do not use the deprecated `/Reports/{id}/Parameters`.
- Run a report with GET `/Reports/{id}/RunReport` or GET `/MemorizedReports/{id}/RunReport`. Parameters are `parameters[n].Parameter` and `parameters[n].Value`. Do not POST. Do not send `orderby`.
- Do not send a date, status, or assignee filter on these reads. Do not combine two filter clauses.

## Do not
- `/Units/search`
- `fields=` combined with `embeds=`
- `$expand=`
- freeform agent-invented filter strings outside this list
- one Balance or RecurringCharges call per unit when a collection embed or a single parent read will do. Tenant balance is the exception: it is GET `/Tenants/{id}/Balance` (a plain JSON number), one call per tenant, not the `Balance` embed.

