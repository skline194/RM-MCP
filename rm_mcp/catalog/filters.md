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
- `PropertyID,eq,{id}` on `/Units` and `/Tenants` for the rent roll
- Do not invent `PropertyID` on `/RecurringCharges` (the catalog marks it read-only with no filters). Use the tenant or unit subresource, or the `RecurringCharges` embed on the parent collection.
- History collections are `/Tenants|Prospects|Owners/{id}/HistoryNotes|HistoryCalls|HistoryEmails|HistoryVisits`. Sort with `orderby=HistoryDate DESC` (this client's parameter; the catalog calls the same idea `orderingOptions`).

## Do not
- `/Units/search`
- `fields=` combined with `embeds=`
- `$expand=`
- freeform agent-invented filter strings outside this list
- one Balance or RecurringCharges call per unit when a collection embed or a single parent read will do. Tenant balance is the exception: it is GET `/Tenants/{id}/Balance` (a plain JSON number), one call per tenant, not the `Balance` embed.

