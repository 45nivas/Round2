# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.325 | -4.8% | 0.226 | -7.6% | 0.514 | +4.6% | 2% | 42 (57) |
| nw_kyc | 0.499 | +36.0% | 0.303 | +25.3% | 0.629 | +5.4% | 29% | 28 (21) |
| nw_renewal | 0.422 | +1.3% | 0.265 | +0.4% | 0.548 | +1.1% | 8% | 39 (54) |
| ALL | 0.394 | +6.0% | 0.254 | +1.4% | 0.553 | +4.4% | 12% | 109 (132) |

## Changes applied by the controller

26 applied — neutral: 10, beneficial: 13, harmful: 3

- nw_renewal v2 (controller_auto) dims=['caller_id', 'schedule'] engageΔ=+0.000 didΔ=-0.007 → **neutral**
- ks_cancel v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=-0.000 didΔ=+0.007 → **neutral**
- ks_cancel v3 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.015 → **neutral**
- ks_cancel v5 (controller_approved) dims=['voice'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.008 didΔ=+0.031 → **beneficial**
- ks_cancel v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.025 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.020 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.016 didΔ=+0.042 → **beneficial**
- ks_cancel v10 (controller_auto) dims=['welcome_message'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.099 didΔ=+0.000 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.042 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- ks_cancel v12 (controller_approved) dims=['welcome_message', 'caller_id'] engageΔ=-0.021 didΔ=+0.035 → **harmful**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.066 didΔ=+0.000 → **harmful**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.043 didΔ=+0.000 → **harmful**
- nw_renewal v10 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.015 → **neutral**
- nw_kyc v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.023 didΔ=+0.000 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.045 didΔ=+0.355 → **beneficial**
- nw_renewal v11 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.021 → **beneficial**
- nw_kyc v5 (controller_approved) dims=['voice'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- nw_kyc v6 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.003 didΔ=-0.000 → **neutral**

## Governance

```
{
  "approval_requests": 15,
  "decisions": {
    "approved": 14,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 14,
  "auto_applied": 12,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "ks_cancel": 3,
    "nw_kyc": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.0,
    "4": 0.0,
    "5": 0.0,
    "6": 0.0,
    "7": 0.0
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0