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

## Do not
- `/Units/search`
- `fields=` combined with `embeds=`
- `$expand=`
- freeform agent-invented filter strings outside this list

