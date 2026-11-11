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
- `PropertyID,eq,{id}` alone on `/Prospects`. Stage, status, lead source, name, and dates are applied in Python.
- `ProspectID,eq,{id}` alone on `/Prospects` for prospect detail.
- `LeaseID,eq,{id}` or `TenantID,eq,{id}` alone on `/Leases` for renewal detail. Do not send both.
- `/ProspectStages`, `/ProspectLostReasons`, `/LeadSources`, `/Appointments`, `/Tasks`, `/Users`, `/Reports`, and `/MemorizedReports` are unfiltered collection reads. Assigned user, due date, appointment date, and active flags are applied in Python.
- A staff member's tasks use GET `/Users/{id}/Tasks` (the id is in the path, not a second filter).
- Report parameters: GET `/Reports/{id}/ReportParameters` or GET `/MemorizedReports/{id}/RunParameters`. Do not use the deprecated `/Reports/{id}/Parameters`.
- Run a report with GET `/Reports/{id}/RunReport` or GET `/MemorizedReports/{id}/RunReport`. Parameters are `parameters[n].Parameter` and `parameters[n].Value`. Do not POST. Do not send `orderby`.
- Do not send a date, status, or assignee filter on these reads. Do not combine two filter clauses.

## Vacancy outlook, delinquency, renewal pipeline (read live 2026-11-10)

These are the filters those tools send. The one-clause rule above still applies to prospect search. Expiring leases and the renewal pipeline send the window filter below.

- `/Units` accepts `Property.IsActive,eq,true` and, when a property is passed, `;PropertyID,eq,{id}`. Embeds `IsVacant,CurrentOccupancyStatus,OccupancyStatusHistory` combine. `IsVacant` is not filterable (400). Decide vacancy in Python.
- Make-ready: `GET /Units/UnitStatuses?filters=UnitID,in,({ids})&embeds=UnitStatusType` for the vacant ids. A 404 falls back to `GET /Units/{id}/UnitStatuses` for vacant units only, capped. Do not rely on `CurrentUnitStatus.UnitStatusType`; it is accepted and comes back empty.
- `GET /MakeReadyPosts` is a small unfiltered read. A 404 is skipped. A 403 is an error.
- Delinquency candidates: `/Tenants` filter `Status,eq,Current;Balance,gt,0;Property.IsActive,eq,true`. `property_id` replaces the active-property clause with `PropertyID,eq,{id}`. `min_balance` raises the `Balance,gt` threshold. `include_past` drops the Status clause. Embeds `Balance` and `OpenReceivables` (and `RecurringCharges` only when rent is requested). The true balance is still `GET /Tenants/{id}/Balance`. `GET /Tenants/{id}/OpenBalance` does not exist.
- Do not embed `LeaseRenewals.LeaseRenewalIncreaseOffer` on `/Leases` (404 KeyNotFoundException). Offers: `GET /LeaseRenewals?filters=ParentLeaseID,in,({ids})&embeds=LeaseRenewalIncreaseOffer`, chunks of 50. A 404 on that batch falls back to `GET /LeaseRenewals/{id}/LeaseRenewalIncreaseOffer`. A 403 is an error.
- Expiring leases and the renewal pipeline send three `/Leases` filters, each plus `PropertyID,eq,{id}` when a property is passed. Term: `ActiveLeaseRenewal.EndDate,ge,{today};ActiveLeaseRenewal.EndDate,le,{today+N}`. Move-out: `MoveOutDate,ge,{today};MoveOutDate,le,{today+N}` and the same range on `ExpectedMoveOutDate`. A month-to-month lease and a lease whose term ends later are included when the move-out is in that range. The same LeaseID is kept once. A 400 on the term filter falls back to the download without date clauses, and a cutoff on that download stays flagged. A 400 on a move-out filter falls back to unit `CurrentOccupancyStatus` (`NoticeDate` or `ExpectedMoveOutDate`). A 403 does not fall back. A lease has ended only when `MoveOutDate` or `DepartureDate` is today or earlier. Blank, `0001-01-01`, and `1900-01-01` are not a move-out. `open_ended` is null on the filtered reads. Do not trust `orderby`. `nn` is not a supported operator.

## Do not
- `/Units/search`
- `fields=` combined with `embeds=`
- `$expand=`
- freeform agent-invented filter strings outside this list
- one Balance or RecurringCharges call per unit when a collection embed or a single parent read will do. Tenant balance is the exception: it is GET `/Tenants/{id}/Balance` (a plain JSON number), one call per tenant, not the `Balance` embed.

