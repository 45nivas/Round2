# Load & Scale Benchmark Report — `public_03_load`

## 1. Scenario Scale
- **Tenants**: 4 (`northwind_life`, `kestrel_finance`, `aravalli_general`, `tidewater_health`)
- **Concurrent Workflows**: 12 (`~650,000` dials/day, `≈ 2 crore` dials/month)
- **History Bootstrap Volume**: `5,537,531` events across 7 history days
- **Live Simulated Volume (2 days)**: `1,872,117` events (`7,409,648` total events processed)

## 2. Measured Controller Performance Metrics

| Metric | Measured Value | Notes |
|---|---|---|
| **History Ingestion Throughput** | **`87,450` events/sec** | `5,537,531` NDJSON events streamed & indexed in `63.32s` |
| **Live Ingestion Throughput** | **`68,751` events/sec** | `1,872,117` live events in `27.23s` |
| **Overall Streaming Throughput** | **`81,827` events/sec** | Combined history + live NDJSON streaming |
| **Initial 12-Workflow Cohort & Schedule Discovery** | **`50.5` ms** | Shared cohort discovery + comparative-advantage hour weights across 12 workflows |
| **Controller Decision Lag (12 workflows/tick)** | **p50: `175.1` ms · p95: `398.7` ms · max: `436.1` ms** | End-to-end statistical evaluation, governance check, and SQLite audit across all 12 workflows |
| **Peak Controller Memory (RSS)** | **`32.0` MB** | Bounded via `bytearray` lead-feature indexing (`1 byte/lead`) and fixed-length sliding `deque`s |
| **Controller Wall-Clock Time / Simulated Day** | **`16.12` s/day** | Pure controller ingestion + decision time (`41.05s/day` including single-threaded simulator physics) |
| **Governance Actions Executed (2 days)** | **`82` audit records (`30` proposals)** | Zero rejected auto-applies, zero conflicts, zero lapsed approvals |

## 3. Architectural Optimizations Enabling High Throughput
1. **1-Byte Interned Lead Feature Array (`WorkflowAccumulator.lead_feat_array`)**:
   - Instead of storing a Python `dict[str, dict[str, str]]` for millions of leads (`~350 bytes/lead`), each unique feature combination `(lang, city_tier, age_band, value_band)` is interned into a 1-byte integer index (`1..84`), and lead IDs (`<wid>:l00012345`) index directly into a contiguous `bytearray`.
   - Memory per 1,000,000 leads is **1.0 MB** instead of `~350 MB`.
2. **Sufficient-Statistic Cell Accumulators & Fixed-Size Deques**:
   - Call events update $O(1)$ sufficient statistics `(dials, connected, engaged, hallucinated)` keyed by `(feat_idx, dim, opt)` plus bounded `deque(maxlen=90)` sliding windows for variant decay and caller-ID health.
   - Decision complexity per tick is $O(|\text{workflows}| \times |\text{feature\_cells}| \times |\text{options}|)$ (`12 × 84 × 4 ≈ 4,032` operations), completely independent of historical call volume $N$.
