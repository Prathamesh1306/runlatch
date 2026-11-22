# Recorded `$0.00` CLI Demo Trace — `runlatch run pr-review --demo`

Command:

```bash
runlatch run pr-review --demo
```

Recorded Terminal Output:

```text
            Workflow Run: pr-review             
     (41dea45c-eec6-4c58-8030-8ad665b2afb4)     
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Metric                    ┃ Value            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ Status                    │ COMPLETED        │
│ Steps Executed            │ 2                │
│ Tool Calls                │ 3                │
│ Tokens (in / out / total) │ 790 / 210 / 1000 │
│ Total Cost (USD)          │ $0.0000          │
└───────────────────────────┴──────────────────┘
                     Executed Tool Calls                      
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ Call ID     ┃ Tool       ┃ Risk Tier ┃ Status    ┃ Latency ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ demo_call_1 │ git_status │ READ      │ SUCCEEDED │    25ms │
│ demo_call_2 │ list_files │ READ      │ SUCCEEDED │   271ms │
│ demo_call_3 │ git_diff   │ READ      │ SUCCEEDED │   137ms │
└─────────────┴────────────┴───────────┴───────────┴─────────┘
╭────────────────────────────── Workflow Output ───────────────────────────────╮
│ ## Autonomous PR Review Report                                               │
│                                                                              │
│ ### Verdict: APPROVED (0 Critical, 0 High, 1 Info)                           │
│                                                                              │
│ | Check | Status | Details |                                                 │
│ |---|---|---|                                                                │
│ | Hexagonal Import Boundaries | PASS | `runlatch/core/` pure domain |        │
│ | Shell & Path Sandbox | PASS | `shell=False` allowlist & path guard |       │
│ | Secret Hygiene | PASS | No hardcoded credentials or unredacted tokens |    │
│ | Stateless HITL Readiness | PASS | `git add -N .` + `git diff HEAD`         │
│ verified |                                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```
