"""Crash-safe SQLite WAL state and decision audit store (C12, C13, C14)."""
import json
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any

from controller.models import IncidentRecord, ProposalPackage


class AuditStore:
    """Persistent SQLite WAL store for controller checkpoints, governance lifecycle, audit trail, and telemetry."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS ingest_checkpoint (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                scenario TEXT NOT NULL,
                sim_minute INTEGER NOT NULL,
                ts TEXT NOT NULL,
                last_history_chunk INTEGER NOT NULL,
                last_event_chunk INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL,
                state_blob BLOB,
                updated_wall_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                approval_id TEXT,
                workflow_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_minute INTEGER NOT NULL,
                created_ts TEXT NOT NULL,
                decided_minute INTEGER,
                decided_ts TEXT,
                applied_minute INTEGER,
                applied_ts TEXT,
                changed_dims_json TEXT NOT NULL,
                change_summary TEXT NOT NULL,
                impact_score REAL NOT NULL,
                affected_leads INTEGER NOT NULL,
                rationale TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                dry_run_json TEXT NOT NULL,
                evidence_json TEXT,
                proposed_config_json TEXT NOT NULL,
                reasons_json TEXT,
                is_urgent INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_proposals_wf_status ON proposals(workflow_id, status);

            CREATE TABLE IF NOT EXISTS decisions_audit (
                decision_id TEXT PRIMARY KEY,
                sim_minute INTEGER NOT NULL,
                ts TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                base_version INTEGER,
                new_version INTEGER,
                proposal_id TEXT,
                approval_id TEXT,
                changed_dims_json TEXT NOT NULL,
                change_summary TEXT NOT NULL,
                rationale TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                dry_run_json TEXT NOT NULL,
                affected_leads INTEGER NOT NULL DEFAULT 0,
                impact_score REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT,
                pre_kpis_json TEXT,
                post_kpis_json TEXT,
                verification_status TEXT DEFAULT 'pending_observation'
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_wf_time ON decisions_audit(workflow_id, sim_minute);

            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                detected_at_minute INTEGER NOT NULL,
                detected_at_ts TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL,
                mitigation_proposal_id TEXT,
                resolved_at_minute INTEGER,
                resolved_at_ts TEXT,
                verification_summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_wf_type ON incidents(workflow_id, incident_type);

            CREATE TABLE IF NOT EXISTS loop_metrics (
                sim_minute INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                wall_elapsed_ms REAL NOT NULL,
                events_ingested INTEGER NOT NULL,
                ingest_eps REAL NOT NULL,
                active_pending_approvals INTEGER NOT NULL,
                decisions_count INTEGER NOT NULL,
                credits_remaining REAL NOT NULL,
                memory_mb REAL NOT NULL,
                alerts_json TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def load_checkpoint(self, expected_scenario: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM ingest_checkpoint WHERE id = 1").fetchone()
        if not row:
            return None
        if row["scenario"] != expected_scenario:
            return None
        state_obj = pickle.loads(row["state_blob"]) if row["state_blob"] else None
        return {
            "scenario": row["scenario"],
            "sim_minute": row["sim_minute"],
            "ts": row["ts"],
            "last_history_chunk": row["last_history_chunk"],
            "last_event_chunk": row["last_event_chunk"],
            "last_event_id": row["last_event_id"],
            "state": state_obj,
        }

    def save_checkpoint(
        self,
        scenario: str,
        sim_minute: int,
        ts: str,
        last_history_chunk: int,
        last_event_chunk: int,
        last_event_id: int,
        state_obj: Any,
    ) -> None:
        blob = pickle.dumps(state_obj, protocol=pickle.HIGHEST_PROTOCOL)
        self.conn.execute(
            """
            INSERT INTO ingest_checkpoint (
                id, scenario, sim_minute, ts, last_history_chunk, last_event_chunk, last_event_id, state_blob, updated_wall_ts
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                scenario = excluded.scenario,
                sim_minute = excluded.sim_minute,
                ts = excluded.ts,
                last_history_chunk = excluded.last_history_chunk,
                last_event_chunk = excluded.last_event_chunk,
                last_event_id = excluded.last_event_id,
                state_blob = excluded.state_blob,
                updated_wall_ts = excluded.updated_wall_ts
            """,
            (scenario, sim_minute, ts, last_history_chunk, last_event_chunk, last_event_id, blob, time.time()),
        )
        self.conn.commit()

    def reset_for_new_world(self) -> None:
        """Clear previous run tables if the simulator started a fresh world on the same DB."""
        cur = self.conn.cursor()
        cur.executescript(
            """
            DELETE FROM ingest_checkpoint;
            DELETE FROM proposals;
            DELETE FROM decisions_audit;
            DELETE FROM incidents;
            DELETE FROM loop_metrics;
            """
        )
        self.conn.commit()

    def record_proposal(
        self,
        pkg: ProposalPackage,
        approval_id: str | None,
        status: str,
        sim_minute: int,
        ts: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO proposals (
                proposal_id, approval_id, workflow_id, tenant_id, base_version, status,
                created_minute, created_ts, changed_dims_json, change_summary,
                impact_score, affected_leads, rationale, diff_json, dry_run_json,
                evidence_json, proposed_config_json, is_urgent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pkg.proposal_id,
                approval_id,
                pkg.workflow_id,
                pkg.tenant_id,
                pkg.base_version,
                status,
                sim_minute,
                ts,
                json.dumps(pkg.changed_dims),
                pkg.change_summary,
                pkg.impact_score,
                pkg.affected_leads,
                pkg.rationale,
                json.dumps(pkg.diff),
                json.dumps(pkg.dry_run),
                json.dumps(pkg.evidence) if pkg.evidence else None,
                json.dumps(pkg.proposed_config),
                1 if pkg.is_urgent_incident else 0,
            ),
        )
        self.conn.commit()

    def update_proposal_status(
        self,
        proposal_id: str,
        status: str,
        sim_minute: int,
        ts: str,
        reasons: list[str] | None = None,
        applied: bool = False,
    ) -> None:
        if applied:
            self.conn.execute(
                """
                UPDATE proposals
                SET status = ?, applied_minute = ?, applied_ts = ?, reasons_json = COALESCE(?, reasons_json)
                WHERE proposal_id = ?
                """,
                (status, sim_minute, ts, json.dumps(reasons) if reasons is not None else None, proposal_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE proposals
                SET status = ?, decided_minute = ?, decided_ts = ?, reasons_json = COALESCE(?, reasons_json)
                WHERE proposal_id = ?
                """,
                (status, sim_minute, ts, json.dumps(reasons) if reasons is not None else None, proposal_id),
            )
        self.conn.commit()

    def get_pending_proposals(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        if workflow_id:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE workflow_id = ? AND status IN ('pending', 'approved')",
                (workflow_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE status IN ('pending', 'approved')"
            ).fetchall()
        return [dict(r) for r in rows]

    def count_recent_requests_24h(self, workflow_id: str, current_minute: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM proposals
            WHERE workflow_id = ? AND approval_id IS NOT NULL AND created_minute > ?
            """,
            (workflow_id, current_minute - 1440),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def record_decision_audit(
        self,
        decision_id: str,
        sim_minute: int,
        ts: str,
        workflow_id: str,
        tenant_id: str,
        decision_type: str,
        actor: str,
        base_version: int | None,
        new_version: int | None,
        proposal_id: str | None,
        approval_id: str | None,
        changed_dims: list[str],
        change_summary: str,
        rationale: str,
        diff: dict[str, Any],
        dry_run: dict[str, Any],
        affected_leads: int = 0,
        impact_score: float = 0.0,
        evidence: dict[str, Any] | None = None,
        pre_kpis: dict[str, Any] | None = None,
        verification_status: str = "pending_observation",
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO decisions_audit (
                decision_id, sim_minute, ts, workflow_id, tenant_id, decision_type, actor,
                base_version, new_version, proposal_id, approval_id, changed_dims_json,
                change_summary, rationale, diff_json, dry_run_json, affected_leads,
                impact_score, evidence_json, pre_kpis_json, verification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                sim_minute,
                ts,
                workflow_id,
                tenant_id,
                decision_type,
                actor,
                base_version,
                new_version,
                proposal_id,
                approval_id,
                json.dumps(changed_dims),
                change_summary,
                rationale,
                json.dumps(diff),
                json.dumps(dry_run),
                affected_leads,
                impact_score,
                json.dumps(evidence) if evidence else None,
                json.dumps(pre_kpis) if pre_kpis else None,
                verification_status,
            ),
        )
        self.conn.commit()

    def update_decision_post_kpis(
        self,
        decision_id: str,
        post_kpis: dict[str, Any],
        verification_status: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE decisions_audit
            SET post_kpis_json = ?, verification_status = ?
            WHERE decision_id = ?
            """,
            (json.dumps(post_kpis), verification_status, decision_id),
        )
        self.conn.commit()

    def record_incident(self, inc: IncidentRecord) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO incidents (
                incident_id, workflow_id, tenant_id, incident_type, detected_at_minute,
                detected_at_ts, status, details_json, mitigation_proposal_id,
                resolved_at_minute, resolved_at_ts, verification_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inc.incident_id,
                inc.workflow_id,
                inc.tenant_id,
                inc.incident_type,
                inc.detected_at_minute,
                inc.detected_at_ts,
                inc.status,
                json.dumps(inc.details),
                inc.mitigation_proposal_id,
                inc.resolved_at_minute,
                inc.resolved_at_ts,
                inc.verification_summary,
            ),
        )
        self.conn.commit()

    def record_loop_metric(
        self,
        sim_minute: int,
        ts: str,
        wall_elapsed_ms: float,
        events_ingested: int,
        ingest_eps: float,
        active_pending_approvals: int,
        decisions_count: int,
        credits_remaining: float,
        memory_mb: float,
        alerts: list[dict[str, Any]],
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO loop_metrics (
                sim_minute, ts, wall_elapsed_ms, events_ingested, ingest_eps,
                active_pending_approvals, decisions_count, credits_remaining, memory_mb, alerts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sim_minute,
                ts,
                wall_elapsed_ms,
                events_ingested,
                ingest_eps,
                active_pending_approvals,
                decisions_count,
                credits_remaining,
                memory_mb,
                json.dumps(alerts),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
