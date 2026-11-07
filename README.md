# Outcome-First Agent Loop — Controller & Evaluation Guide

Always-on, crash-resilient HTTP controller (`controller/`) that drives outbound voice workflows across cohort discovery, hourly call-time optimization, caller-ID spam isolation, Empirical Bayes variant experimentation, decay rollback, hallucination incident mitigation, and strict client governance, backed by an embedded SQLite WAL audit store (`controller_state.db`).

- **Design & Stakeholder Write-Up:** [SUBMISSION.md](SUBMISSION.md)
- **Evaluation & Benchmark Reports:** [reports/public_01.md](reports/public_01.md) · [reports/public_02.md](reports/public_02.md) · [reports/public_03_load.md](reports/public_03_load.md) · [reports/crash_recovery.log](reports/crash_recovery.log)

### Headline Results

| Scenario / Test | Key Results | Governance & Safety Hygiene |
|---|---|---|
| `public_01_dev` | **+6.0%** ALL conclusion (`+36.0%` `nw_kyc`), **+4.4%** engagement; decaying `W3` purged to **`0.0` share** on Days 3–7 | `0` conflicts · `0` lapsed · `0` expired · `0` rejected auto-applies |
| `public_02_incidents` | **+26.5%** ALL conclusion (`+39.1%` `ks_cancel`, `+26.5%` `nw_kyc`, `+17.2%` `nw_renewal`), **+22.0%** pickup; hallucinated calls cut **`431 → 151`** (`ks_cancel`: `348 → 71`) | Flagged `D2` at **`0.0` share** Days 3–9 · `P2 → P3` resolved in **`120m` MTTR** spending **`6 / 200` credits** |
| `public_03_load` | **`87,450` events/sec** history ingest · **`68,751` events/sec** live ingest · **`p95 = 398.7 ms`** 12-workflow tick lag · **`32.0 MB` RSS** peak RAM · **`16.12 s/day`** controller wall time | `0` conflicts · `0` lapsed · `0` rejected auto-applies across 12 workflows |
| Crash Recovery | Hard kill (`os._exit(137)`) at `sim_minute=4800` and resume from SQLite WAL to `sim_minute=11520` | `0` duplicate chunks · `0` conflicts · `0` lost approvals |

---

## Quickstart & Runbook

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```
*(Zero extra third-party packages required — built strictly on Python 3.11+ standard library + `httpx`.)*

### 2. Run a Scenario End-to-End
```bash
# 1. Generate no-op baseline
python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base02

# 2. Start simulator HTTP server (Terminal 1, default :8700)
python -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/mine02 --port 8700 --force

# 3. Run the controller until scenario completion (Terminal 2 — zero manual steps)
./run.sh http://127.0.0.1:8700
# Cross-platform / custom DB path alternative:
# python -m controller --url http://127.0.0.1:8700 --db controller_state.db

# 4. Grade controller run against baseline
python -m sim grade --run runs/mine02 --baseline runs/base02 --out reports/public_02.md
```

### 3. Audit, Supervisor & Health CLI (`controller.cli`)
Query the controller's own SQLite WAL store (`controller_state.db`) at any time during or after a run:
```bash
# Explain every change on a workflow: who approved it, rationale, validation evidence, diff, dry-run & post-apply KPIs
python -m controller.cli explain --db controller_state.db --workflow ks_cancel
python -m controller.cli explain --db controller_state.db --workflow ks_cancel --at 2027-01-07T12:00

# Single-screen consolidated operations view across all workflows (pending requests, 24h changes, incidents)
python -m controller.cli supervisor-view --db controller_state.db

# Loop observability: total events ingested, p50/p95/max tick latency, peak RSS memory, remaining credits & alerts
python -m controller.cli health --db controller_state.db
```

### 4. Tests, Crash-Recovery Proof & Scale Benchmark
```bash
# Run simulator + controller unit/integration test suite
python -m pytest -v tests

# Demonstrate mid-run hard kill (exit 137 at t=4800m) and seamless SQLite WAL restart
PYTHONPATH=. python scripts/demonstrate_crash_recovery.py

# Run 12-workflow load benchmark (scenarios/public_03_load.yaml) and write reports/public_03_load.md
PYTHONPATH=. python scripts/benchmark_load.py
```

### 5. Controller Architecture & Extension Guide
```text
controller/
├── __main__.py          # Entrypoint (`python -m controller --url <SIM_URL>`)
├── config.py            # Statistical thresholds, canary floors, governance & credit guardrails
├── models.py            # Shared immutable Cohort abstraction, WorkflowStats, ProposalDraft
├── store.py             # Crash-safe SQLite WAL checkpoint, proposal, incident & decision audit store
├── ingestor.py          # Streaming NDJSON ingestor + 1-byte bytearray lead feature interning
├── cohort_engine.py     # Day-0 history bootstrapper, non-fragmenting cohort discovery & hour_weights
├── stats_engine.py      # Empirical Bayes shrinkage, Benjamini-Hochberg FDR, decay & peer-DID detectors
├── incident_manager.py  # Sequential prompt hallucination detector, /llm fix scorer & /validation runner
├── governance.py        # Proposal state machine, fatigue/window scheduler, TVD auto-apply & replay dry-run
├── engine.py            # Main tick orchestration, supervisor override grace window & post-apply verifier
└── cli.py               # `explain`, `supervisor-view`, and `health` operator CLI commands
```
- **Tuning statistical sensitivity or credit floors:** edit `ControllerParams` in `controller/config.py`.
- **Adding a new lead feature dimension or cohort rule:** extend `Cohort.matches()` in `controller/models.py` and candidate generation in `controller/cohort_engine.py`.
- **Adding a new anomaly detector:** implement the check over `WorkflowStats` in `controller/stats_engine.py` or `controller/incident_manager.py` and route resulting target diffs through `GovernanceManager` (`controller/governance.py`).

---

# Outcome-First Agent Loop — Senior Engineer Assignment (Original Specification)

**Formi · Agentic Universe** · Greenfield · Suggested effort 12–16 hours across 7 calendar days

---

## 1. Context

Formi runs outbound voice agents for insurers and lenders across India: renewal reminders, KYC follow-ups,
cancellation reconfirmations. Each **workflow** dials leads within a business-set window, following a retry
cadence the business configures. Today a human sets the agent's configuration once and rarely revisits it.
That configuration covers which welcome message it opens with, which voice it uses, which caller ID it dials
from, which prompt version runs, and which hours each kind of person gets called in.

Two numbers decide whether a workflow is healthy, and we never blend them:

- **Pickup rate**: of the dials, how many connected.
- **Engagement rate**: of the people who picked up, how many stayed to a conclusion (interested *or* not).

The business outcome sits above both: **leads concluded ÷ leads uploaded**.

We are moving from agents that humans tune to agents that **earn autonomy**. They notice what's working, try
better options on the right people, ask for permission in a form a busy supervisor can say yes to, apply
changes safely, and back out when something stops working. A client supervisor can see why every change was
made. Nothing changes on a live client's agent without passing the governance boundary.

**Your job is to build that loop.**

## 2. The problem

Build a **controller**: an always-on service that, for every workflow on the platform, continuously:

1. **Detects**: pickup or engagement drifting, a caller ID going bad, hallucinations spiking. It works on
   thresholds and populations, never on single calls.
2. **Hypothesises and experiments**: it forms cohorts, splits traffic between variants, and decides how much
   evidence is enough.
3. **Proposes**: it packages a change with a summary, a diff, the affected leads, a dry run, an impact score
   and a rationale, then routes it through governance.
4. **Applies and learns**: it applies what's approved, keeps measuring, promotes winners and rolls back
   variants that degrade.
5. **Explains**: from your system's own records, anyone can answer *"why did workflow X change at 14:00 on
   D3, who approved it, and what happened next?"*

You won't connect to our production. `sim/` contains a **closed-loop simulator** of the world: tenants,
workflows, leads, a dialler, people who pick up or don't, client approvers, client supervisors who edit things
mid-day, and an LLM that suggests prompt fixes. Your controller drives it over HTTP. The simulator holds
hidden ground truth: which variants actually work, for whom, for how long, and what happens when you change
things. Read **[docs/SIMULATOR.md](docs/SIMULATOR.md)** carefully. It is the contract.

## 3. Scale envelope

Design for the platform we're building toward, not the dev scenario:

- **~2 crore dials/month**: about 6.5 lakh/day inside a 12-hour window, peaking above 50k dials/hour.
- Tens of tenants and hundreds of workflows, each with its own policy, objective and approver.
- Events arrive continuously. Your loop's decisions are only as fresh as its ingestion.

`scenarios/public_03_load.yaml` (12 workflows, ~650k dials/day, ~850k events/day) proves your system keeps up.
We will ask for numbers: ingest throughput, decision lag, memory, and where it would break at 10×.

## 4. What's in the box

```
sim/                      the world (don't modify — we run our own copy)
scenarios/
  public_01_dev.yaml      3 workflows, 8 days, no incidents — start here
  public_02_incidents.yaml  caller ID flagged, a supervisor ships a bad prompt, a supervisor edits mid-experiment
  public_03_load.yaml     12 workflows at full scale, 5 days
stakeholders/             six memos from people who want things from you. They disagree.
examples/naive_controller.py   shows the API round-trip. It is deliberately bad.
docs/SIMULATOR.md         API, config, governance, events
SUBMISSION_TEMPLATE.md    copy to SUBMISSION.md
```

```bash
pip install -r requirements.txt
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base01
python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01     # :8700
./run.sh http://127.0.0.1:8700                                                        # your controller
python -m sim grade --run runs/mine01 --baseline runs/base01 --out reports/public_01.md
```

**Ground rules for the simulator**
- Your controller may only know what the HTTP API tells it. Reading `runs/`, importing `sim`, or parsing
  scenario files (including fields like `base_pickup` or `did_base_health`, which the API never returns) is
  disqualifying. Our hidden scenarios share the schema and
  differ in truth, incidents and policy. A controller tuned to the public truth will do badly on them.
- `./run.sh <SIM_URL>` must start your controller and run until the scenario finishes, with no manual steps.
  We will run it against hidden scenarios on a 4-vCPU / 8 GB Linux box. Docker is fine. Say so in `run.sh`.
- Any language and any storage. Every dependency you add is something you'll have to defend
  (see `stakeholders/03`).

## 5. Constraints that are not up for negotiation

These come from the platform and our regulated clients. They don't move, whatever the memos say.

1. **The governance boundary is absolute.** Every change your controller makes goes through `/approvals` or a
   policy-permitted auto-apply. Supervisor edits are a fact of life. You don't overwrite them silently.
2. **The business owns whether and when to call.** Cadence, dialling windows and capacity are out of scope.
   You decide *who* gets called in a given hour and *how* the agent shows up.
3. **Pickup and engagement stay separate KPIs.** Optimise a workflow's `primary` KPI and respect its
   `guardrail`.
4. **Winner selection is statistical and deterministic.** An LLM may propose what to try. It never decides
   what won.
5. **Act on populations, not anecdotes.** Detection is threshold-based. No change is triggered by one call.
6. **Every decision is auditable** from your own store. The simulator's logs don't count.
7. **Credits are finite.** LLM and validation calls cost credits. Running out mid-incident is a failure.
8. **Crash safety.** Kill your controller at any point, restart it, and it resumes. It must not double-apply,
   lose a pending approval, or forget what it learned.

## 6. Scope: you can't do all of it

Here is everything someone has asked for. Done well, it's **2–3× what fits in the time budget.** Choosing is
part of the assignment. Don't spread yourself thin. We'd rather see five capabilities done properly than
fifteen sketched.

| # | Capability |
|---|---|
| C1 | Event ingestion at load-scenario scale: backlog-safe, idempotent, restartable |
| C2 | Lead/feature joins and **one shared cohort object** used by detection, experiments and the supervisor view |
| C3 | Bootstrapping from history before D0 |
| C4 | Call-time optimisation: per cohort and/or per person |
| C5 | Caller-ID health: detect degradation, rotate, explore safely |
| C6 | Variant experimentation on welcome message and voice: design, minimum sample sizes, multiple comparisons |
| C7 | Winner promotion, **decay detection and rollback** |
| C8 | Hallucination incident response: detect → LLM suggest → validate → propose with evidence → verify after apply |
| C9 | Governance client: proposal lifecycle, batching under approver fatigue, timing against approver hours, expiry/lapse, stale-base re-proposal, auto-apply within policy |
| C10 | Honest impact scores, affected-lead counts, and dry runs against historical cohorts |
| C11 | A consolidated supervisor view (API or minimal UI): one place, not fifty alerts |
| C12 | A queryable decision/audit store: *"why did X change"* |
| C13 | Crash recovery, demonstrated: kill mid-run, restart, show the run completes correctly |
| C14 | Observability of the loop itself: ingest lag, decisions/hour, credit burn, alerts an on-call engineer can act on |
| C15 | Fair allocation of credits and approver attention across tenants |

**Floor (mandatory):** the controller runs public_01 and public_02 end to end with no manual steps, keeps an
audit trail (C12, even a minimal one), never breaks the constraints above, and `SUBMISSION.md` is complete.
Everything beyond that is your call. Write down what you cut and why.

## 7. Stakeholders

`stakeholders/` holds six memos: Sales, a client's Compliance head, Platform/SRE, a client operations
supervisor, Product, and the CTO. Each is reasonable on its own. Together they contradict each other, and
some assume things about the system that aren't true. **Check their claims against the actual workflow
policies.** In `SUBMISSION.md`, cover each one: what you accepted, what you pushed back on, and the reply
you'd actually send them.

## 8. Deliverables

A git repository (private is fine; share access with the email on the form) containing:

1. **Your controller**, with `run.sh`.
2. **`SUBMISSION.md`**, from the template. This is where most of the evaluation happens.
3. **`reports/`**: `sim grade` output for public_01 and public_02, plus your load-test numbers for public_03.
4. **Crash-recovery evidence**: a script or log showing a kill/restart mid-run and a correct finish.
5. **Commit history** that shows how your thinking progressed. Small, honest commits beat one squash.
6. *Optional:* a walkthrough video of 5 minutes or less.

## 9. How we evaluate

| Weight | What | How |
|---|---|---|
| 25% | **Outcomes on hidden scenarios** | We run `run.sh` against 2 hidden scenarios. We look at lift versus baseline on the primary KPI (with the guardrail respected), harmful changes applied, incident time-to-fix, decay rollback, and governance hygiene. No points for lift bought by breaking a constraint. |
| 25% | **Design and reasoning** | Is the learning loop statistically sound? Are cohorts sized sensibly? Are false positives handled? Does the design hold at the scale envelope? |
| 20% | **Scope and stakeholder judgement** | Did you choose what mattered, say no clearly, and catch the memos' wrong assumptions? |
| 20% | **Engineering** | Correctness, crash safety, idempotency, throughput on the load scenario, tests where they matter, and code a teammate could own. |
| 10% | **Communication** | Could a new engineer run, debug and extend this from your documents alone? |

A 60-minute review follows. We'll pick one decision your controller made on a hidden scenario and ask you to
explain it from your audit store, live. We'll change a constraint and ask what breaks. We'll ask about the
thing you cut.

## 10. Rules

- **AI tools are allowed, and we expect you to use them.** You must be able to defend every line and every
  decision without them in the review. Generic, unadapted output is easy to spot and scores poorly.
- If something is ambiguous, **state your assumption in `SUBMISSION.md` and proceed**. Don't wait for an
  answer. Questions are welcome but won't block you.
- Don't share the assignment or your solution publicly.
