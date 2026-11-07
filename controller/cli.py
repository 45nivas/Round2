"""Audit & Supervisor CLI (`python -m controller.cli explain|supervisor-view|health`) for C11, C12, C14."""
import argparse
import json
import sys
from typing import Any

from controller.store import AuditStore


def cmd_explain(store: AuditStore, workflow_id: str, at_ts: str | None = None) -> str:
    """Answer 'why did workflow X change, who approved it, and what happened after?' from audit records alone (C12)."""
    lines: list[str] = [
        f"================================================================================",
        f"AUDIT EXPLANATION REPORT — workflow={workflow_id}" + (f" (filter at={at_ts})" if at_ts else ""),
        f"================================================================================",
    ]

    # Fetch applied/recorded decisions for workflow_id
    query = """
        SELECT d.*, p.status AS prop_status, p.decided_ts, p.applied_ts, p.reasons_json
        FROM decisions_audit d
        LEFT JOIN proposals p ON d.proposal_id = p.proposal_id
        WHERE d.workflow_id = ?
        ORDER BY d.sim_minute ASC, d.rowid ASC
    """
    rows = store.conn.execute(query, (workflow_id,)).fetchall()
    if at_ts:
        rows = [r for r in rows if at_ts in str(r["ts"]) or (r["applied_ts"] and at_ts in str(r["applied_ts"]))]

    if not rows:
        lines.append(f"No matching decision audit records found for workflow={workflow_id}.")
        return "\n".join(lines)

    for idx, r in enumerate(rows, start=1):
        dec_type = r["decision_type"]
        actor = r["actor"]
        ts = r["ts"]
        sim_min = r["sim_minute"]
        b_ver = r["base_version"]
        n_ver = r["new_version"]
        pid = r["proposal_id"] or "N/A"
        aid = r["approval_id"] or ("auto-policy" if actor == "controller_auto" else "external-supervisor")
        dims = json.loads(r["changed_dims_json"])
        diff = json.loads(r["diff_json"])
        dry_run = json.loads(r["dry_run_json"])
        pre_kpis = json.loads(r["pre_kpis_json"]) if r["pre_kpis_json"] else {}
        post_kpis = json.loads(r["post_kpis_json"]) if r["post_kpis_json"] else {}
        evidence = json.loads(r["evidence_json"]) if r["evidence_json"] else None

        ver_str = f"v{b_ver} -> v{n_ver}" if n_ver is not None else f"base v{b_ver} (request)"
        lines.append(f"\n[{idx}] {ts} (t={sim_min}m) | {dec_type.upper()} | {ver_str}")
        lines.append(f"    Who Approved / Actor : {actor} (approval_id={aid}, proposal_id={pid})")
        lines.append(f"    Changed Dimensions   : {dims}")
        lines.append(f"    Summary              : {r['change_summary']}")
        lines.append(f"    Why (Rationale)      : {r['rationale']}")
        if evidence:
            lines.append(f"    Validation Evidence  : {json.dumps(evidence)}")
        lines.append(f"    Config Diff          : {json.dumps(diff)}")
        if dry_run:
            lines.append(
                f"    Dry-Run Projection   : pickup {dry_run.get('baseline_pickup_rate')} -> "
                f"{dry_run.get('projected_pickup_rate')} ({dry_run.get('expected_pickup_delta', 0):+.4f}), "
                f"engagement {dry_run.get('baseline_engagement_rate')} -> "
                f"{dry_run.get('projected_engagement_rate')} ({dry_run.get('expected_engagement_delta', 0):+.4f}) "
                f"[affected_leads={r['affected_leads']}, impact={r['impact_score']}]"
            )
        if pre_kpis:
            lines.append(
                f"    Pre-Change KPIs      : pickup={pre_kpis.get('pickup_rate')}, "
                f"engagement={pre_kpis.get('engagement_rate')}, "
                f"hallucination={pre_kpis.get('hallucination_rate')} (dials={pre_kpis.get('dials')})"
            )
        if post_kpis:
            lines.append(
                f"    What Happened After  : pickup={post_kpis.get('pickup_rate')}, "
                f"engagement={post_kpis.get('engagement_rate')}, "
                f"hallucination={post_kpis.get('hallucination_rate')} "
                f"(verified on {post_kpis.get('sample_dials')} subsequent calls — status={r['verification_status']})"
            )
        else:
            lines.append(f"    What Happened After  : status={r['verification_status']}")

    # Include related incidents on this workflow
    inc_rows = store.conn.execute(
        "SELECT * FROM incidents WHERE workflow_id = ? ORDER BY detected_at_minute ASC",
        (workflow_id,),
    ).fetchall()
    if inc_rows:
        lines.append(f"\n--------------------------------------------------------------------------------")
        lines.append(f"INCIDENT TIMELINE FOR {workflow_id}:")
        for inc in inc_rows:
            lines.append(
                f"  - [{inc['detected_at_ts']}] {inc['incident_type']} (id={inc['incident_id']}, status={inc['status']})"
            )
            lines.append(f"    Details     : {inc['details_json']}")
            if inc["mitigation_proposal_id"]:
                lines.append(f"    Mitigation  : proposal_id={inc['mitigation_proposal_id']}")
            if inc["verification_summary"]:
                lines.append(f"    Post-Verify : {inc['verification_summary']} (resolved at {inc['resolved_at_ts']})")

    return "\n".join(lines)


def cmd_supervisor_view(store: AuditStore) -> str:
    """Render consolidated single-screen supervisor summary across all workflows (C11)."""
    ckpt = store.conn.execute("SELECT * FROM ingest_checkpoint WHERE id = 1").fetchone()
    sim_ts = ckpt["ts"] if ckpt else "N/A"
    sim_min = int(ckpt["sim_minute"]) if ckpt else 0

    lines: list[str] = [
        "================================================================================",
        f"SUPERVISOR CONSOLIDATED OPERATIONS VIEW (as of {sim_ts}, sim_minute={sim_min})",
        "================================================================================",
    ]

    # 1. Pending approval requests ordered by impact_score DESC
    pending = store.conn.execute(
        "SELECT * FROM proposals WHERE status = 'pending' ORDER BY is_urgent DESC, impact_score DESC"
    ).fetchall()
    lines.append(f"\n1. PENDING APPROVAL REQUESTS ({len(pending)} active):")
    if not pending:
        lines.append("   None — inbox clear.")
    for p in pending:
        urg = "[URGENT INCIDENT] " if p["is_urgent"] else ""
        lines.append(
            f"   * {urg}{p['workflow_id']} | {p['approval_id']} | impact={p['impact_score']:.2f} | "
            f"affected_leads={p['affected_leads']} | {p['change_summary']}"
        )

    # 2. Recent changes applied in last 24h
    recent_changes = store.conn.execute(
        """
        SELECT * FROM decisions_audit
        WHERE decision_type IN ('auto_apply', 'approval_applied', 'supervisor_override')
          AND sim_minute >= ?
        ORDER BY sim_minute DESC
        """,
        (max(0, sim_min - 1440),),
    ).fetchall()
    lines.append(f"\n2. CHANGES APPLIED IN LAST 24 HOURS ({len(recent_changes)}):")
    for c in recent_changes:
        lines.append(
            f"   * [{c['ts']}] {c['workflow_id']} v{c['base_version']}->v{c['new_version']} "
            f"({c['actor']}): {c['change_summary']}"
        )

    # 3. Active & resolved incidents
    incidents = store.conn.execute("SELECT * FROM incidents ORDER BY detected_at_minute DESC").fetchall()
    lines.append(f"\n3. INCIDENTS & SAFETY MONITORS ({len(incidents)} total):")
    for inc in incidents:
        lines.append(
            f"   * [{inc['detected_at_ts']}] {inc['workflow_id']} | {inc['incident_type']} | "
            f"status={inc['status']} | {inc['verification_summary'] or inc['details_json']}"
        )

    return "\n".join(lines)


def cmd_health(store: AuditStore) -> str:
    """Report C14 loop observability metrics and alert status."""
    rows = store.conn.execute("SELECT * FROM loop_metrics ORDER BY sim_minute ASC").fetchall()
    if not rows:
        return "No loop metrics recorded."

    latencies = sorted(float(r["wall_elapsed_ms"]) for r in rows)
    total_ev = sum(int(r["events_ingested"]) for r in rows)
    peak_mem = max(float(r["memory_mb"]) for r in rows)
    last = rows[-1]
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    return "\n".join(
        [
            "================================================================================",
            f"CONTROLLER LOOP HEALTH & OBSERVABILITY (last_ts={last['ts']}, ticks={len(rows)})",
            "================================================================================",
            f"  Total Events Ingested : {total_ev:,}",
            f"  Tick Latency (ms)     : p50={p50:.2f} ms | p95={p95:.2f} ms | max={max(latencies):.2f} ms",
            f"  Peak Memory RSS       : {peak_mem:.1f} MB",
            f"  Credits Remaining     : {last['credits_remaining']:.1f}",
            f"  Total Audit Decisions : {last['decisions_count']}",
            f"  Active Alerts         : {last['alerts_json']}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit & Supervisor CLI for the Outcome-First Controller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("explain", help="Explain why a workflow changed, who approved it, and what happened after")
    p_exp.add_argument("--db", default="controller_state.db", help="Path to SQLite audit DB")
    p_exp.add_argument("--workflow", required=True, help="Workflow ID (e.g. ks_cancel, nw_renewal, nw_kyc)")
    p_exp.add_argument("--at", default=None, help="Optional timestamp substring filter")

    p_sup = sub.add_parser("supervisor-view", help="Consolidated supervisor view across workflows")
    p_sup.add_argument("--db", default="controller_state.db", help="Path to SQLite audit DB")

    p_hlt = sub.add_parser("health", help="Loop health & observability metrics")
    p_hlt.add_argument("--db", default="controller_state.db", help="Path to SQLite audit DB")

    args = parser.parse_args(argv)
    store = AuditStore(args.db)
    try:
        if args.cmd == "explain":
            print(cmd_explain(store, args.workflow, args.at))
        elif args.cmd == "supervisor-view":
            print(cmd_supervisor_view(store))
        elif args.cmd == "health":
            print(cmd_health(store))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
