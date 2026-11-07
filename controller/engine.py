"""Main Outcome-First Controller loop orchestrating ingestion, optimization, incidents, and governance."""
import copy
import os
import time
import uuid
from pathlib import Path
from typing import Any
import httpx

from controller.cohort_engine import CohortEngine
from controller.config import ControllerConfig, DEFAULT_CONFIG
from controller.governance import (
    GovernanceManager,
    compute_changed_dims,
    compute_config_diff,
    total_variation_distance,
)
from controller.incident_manager import IncidentManager
from controller.ingestor import EventIngestor
from controller.models import Cohort, IncidentRecord, ProposalPackage
from controller.stats_engine import StatsEngine
from controller.store import AuditStore


def _get_memory_mb() -> float:
    """Return approximate resident memory usage in MB across Linux/macOS/Windows."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return float(pmc.WorkingSetSize) / (1024.0 * 1024.0)
    except Exception:
        pass
    return 32.0


class ControllerEngine:
    """Always-on HTTP controller loop driving the simulator from start to completion."""

    def __init__(
        self,
        sim_url: str,
        db_path: str | Path = "controller_state.db",
        cfg: ControllerConfig = DEFAULT_CONFIG,
        kill_at_minute: int | None = None,
    ):
        self.sim_url = sim_url.rstrip("/")
        self.cfg = cfg
        self.kill_at_minute = kill_at_minute

        self.store = AuditStore(db_path)
        self.ingestor = EventIngestor(cfg)
        self.cohort_engine = CohortEngine(cfg)
        self.stats_engine = StatsEngine(cfg)
        self.incident_manager = IncidentManager(self.store, cfg)
        self.governance = GovernanceManager(self.store, cfg)

        self.workflows_meta: dict[str, dict[str, Any]] = {}
        self.discovered_cohorts: dict[str, list[Cohort]] = {}
        self.target_hour_weights: dict[str, dict[str, dict[str, float]]] = {}
        self.reported_degraded_dids: dict[str, set[str]] = {}
        self.reported_decayed_variants: dict[str, set[tuple[str, str]]] = {}
        self.credits_remaining: float = 200.0
        self.scenario: str = ""

    def run(self) -> dict[str, Any]:
        """Run the controller loop against `self.sim_url` until `finished` is True."""
        with httpx.Client(base_url=self.sim_url, timeout=60.0) as client:
            state = None
            for attempt in range(60):
                try:
                    resp = client.get("/sim/state")
                    if resp.status_code == 200:
                        state = resp.json()
                        break
                except httpx.HTTPError:
                    if attempt == 59:
                        raise
                    time.sleep(0.5)
            assert state is not None, "Failed to connect to simulator /sim/state"

            self.scenario = str(state["scenario"])
            sim_minute = int(state["sim_minute"])
            ts = str(state["ts"])
            self.credits_remaining = float(state.get("credits_remaining", 200.0))

            wfs = client.get("/workflows").json()
            self.workflows_meta = {str(w["workflow_id"]): w for w in wfs}

            # 1. Initialize or recover from crash checkpoint (C13)
            recovered = self._initialize_or_recover(client, self.scenario, sim_minute)

            # 2. Ingest any available history and live chunks
            t0 = time.perf_counter()
            h_ev, _ = self.ingestor.ingest_available_chunks(client, kind="history")
            l_ev, _ = self.ingestor.ingest_available_chunks(client, kind="events")
            init_elapsed = max(1e-6, time.perf_counter() - t0)

            # 3. Discover shared Cohort definitions (C2, C3) and compute initial hour_weights (C4)
            for wid, wf_meta in self.workflows_meta.items():
                if wid not in self.discovered_cohorts or not self.discovered_cohorts[wid]:
                    self.discovered_cohorts[wid] = self.cohort_engine.discover_cohorts(
                        self.ingestor, wid, wf_meta
                    )
                if wid not in self.target_hour_weights or not self.target_hour_weights[wid]:
                    self.target_hour_weights[wid] = self.cohort_engine.compute_hour_weights(
                        self.ingestor, wid, wf_meta, self.discovered_cohorts[wid]
                    )
                self.reported_degraded_dids.setdefault(wid, set())
                self.reported_decayed_variants.setdefault(wid, set())

            # 4. At D0 00:00 (fresh start), immediately auto-apply default schedule.hour_weights where permitted
            if sim_minute == 0 and not recovered:
                self._apply_initial_day0_auto_configs(client, sim_minute, ts)

            self._save_checkpoint(self.scenario, sim_minute, ts)
            self._record_tick_telemetry(
                sim_minute=sim_minute,
                ts=ts,
                wall_elapsed_ms=init_elapsed * 1000.0,
                events_ingested=h_ev + l_ev,
                ingest_eps=(h_ev + l_ev) / init_elapsed,
                credits_remaining=self.credits_remaining,
            )

            # 5. Main tick loop
            while not (state.get("finished", False) or state.get("done", False)):
                sim_minute = int(state["sim_minute"])
                if self.kill_at_minute is not None and sim_minute >= self.kill_at_minute:
                    self._save_checkpoint(self.scenario, sim_minute, str(state["ts"]))
                    self.store.close()
                    os._exit(137)

                step_mins = self._compute_advance_minutes(sim_minute)
                tick_t0 = time.perf_counter()

                adv_resp = client.post("/sim/advance", json={"minutes": step_mins})
                if adv_resp.status_code == 409:
                    state["finished"] = True
                    break
                adv_resp.raise_for_status()
                state = adv_resp.json()
                sim_minute = int(state["sim_minute"])
                ts = str(state["ts"])

                # Stream newly closed event chunks
                new_chunks = state.get("new_chunks", [])
                ev_count, ing_sec = self.ingestor.ingest_available_chunks(
                    client, kind="events", chunk_Descriptors=new_chunks
                )

                # Process config_changed & supervisor edits
                self._process_config_events(client, sim_minute, ts)

                # Reconcile pending approvals & immediately apply any approved changes
                self.governance.reconcile_and_apply_approvals(
                    client, self.ingestor, self.workflows_meta, sim_minute, ts
                )

                # Per-workflow optimization, incident response, and governance routing
                for wid, wf_meta in self.workflows_meta.items():
                    self._evaluate_workflow(client, wid, wf_meta, sim_minute, ts)

                # Update post-apply KPIs on earlier decisions once enough post-change calls accumulate
                self._update_post_decision_kpis(sim_minute)

                wall_ms = (time.perf_counter() - tick_t0) * 1000.0
                self._save_checkpoint(self.scenario, sim_minute, ts)
                self._record_tick_telemetry(
                    sim_minute=sim_minute,
                    ts=ts,
                    wall_elapsed_ms=wall_ms,
                    events_ingested=ev_count,
                    ingest_eps=ev_count / max(1e-6, ing_sec),
                    credits_remaining=self.credits_remaining,
                )

            final_state = client.get("/sim/state").json()
            self.credits_remaining = float(final_state.get("credits_remaining", self.credits_remaining))
            return final_state

    def _initialize_or_recover(self, client: httpx.Client, scenario: str, sim_minute: int) -> bool:
        """Restore checkpoint state on mid-run restart, or reset DB if simulator started a fresh world (C13)."""
        ckpt = self.store.load_checkpoint(scenario)
        if ckpt is None:
            self.store.reset_for_new_world()
            self._sync_live_workflow_versions(client, sim_minute)
            return False

        if sim_minute < int(ckpt["sim_minute"]):
            self.store.reset_for_new_world()
            self._sync_live_workflow_versions(client, sim_minute)
            return False

        pending = self.store.get_pending_proposals()
        if sim_minute == 0 and ckpt["sim_minute"] > 0:
            self.store.reset_for_new_world()
            self._sync_live_workflow_versions(client, sim_minute)
            return False
        if pending:
            sample_aid = pending[0].get("approval_id")
            if sample_aid and client.get(f"/approvals/{sample_aid}").status_code == 404:
                self.store.reset_for_new_world()
                self._sync_live_workflow_versions(client, sim_minute)
                return False

        blob_state = ckpt.get("state") or {}
        if "ingestor" in blob_state:
            self.ingestor.restore_state(blob_state["ingestor"])
        if "discovered_cohorts" in blob_state:
            self.discovered_cohorts = {
                wid: [Cohort.from_dict(c) for c in clist]
                for wid, clist in blob_state["discovered_cohorts"].items()
            }
        if "target_hour_weights" in blob_state:
            self.target_hour_weights = blob_state["target_hour_weights"]
        if "last_proposal_minute" in blob_state:
            self.governance.last_proposal_minute = blob_state["last_proposal_minute"]
        if "last_auto_apply_minute" in blob_state:
            self.governance.last_auto_apply_minute = blob_state["last_auto_apply_minute"]
        if "active_incidents" in blob_state:
            self.incident_manager.active_incidents = blob_state["active_incidents"]
        if "validated_prompts" in blob_state:
            self.incident_manager.validated_prompts = blob_state["validated_prompts"]
        if "reported_degraded_dids" in blob_state:
            self.reported_degraded_dids = blob_state["reported_degraded_dids"]
        if "reported_decayed_variants" in blob_state:
            self.reported_decayed_variants = blob_state["reported_decayed_variants"]

        self._sync_live_workflow_versions(client, sim_minute)
        return True

    def _sync_live_workflow_versions(self, client: httpx.Client, sim_minute: int) -> None:
        for wid in self.workflows_meta:
            resp = client.get(f"/workflows/{wid}/config").json()
            acc = self.ingestor.get_workflow(wid)
            acc.last_config_version = int(resp["version"])
            cur_pv = str(resp["config"].get("prompt_version", "P1"))
            if acc.active_prompt_version != cur_pv:
                acc.reset_prompt_epoch(cur_pv, sim_minute)

    def _save_checkpoint(self, scenario: str, sim_minute: int, ts: str) -> None:
        state_obj = {
            "ingestor": self.ingestor.export_state(),
            "discovered_cohorts": {
                wid: [c.to_dict() for c in clist] for wid, clist in self.discovered_cohorts.items()
            },
            "target_hour_weights": self.target_hour_weights,
            "last_proposal_minute": self.governance.last_proposal_minute,
            "last_auto_apply_minute": self.governance.last_auto_apply_minute,
            "active_incidents": self.incident_manager.active_incidents,
            "validated_prompts": self.incident_manager.validated_prompts,
            "reported_degraded_dids": self.reported_degraded_dids,
            "reported_decayed_variants": self.reported_decayed_variants,
        }
        self.store.save_checkpoint(
            scenario=scenario,
            sim_minute=sim_minute,
            ts=ts,
            last_history_chunk=self.ingestor.last_history_chunk,
            last_event_chunk=self.ingestor.last_event_chunk,
            last_event_id=self.ingestor.last_event_id,
            state_obj=state_obj,
        )

    def _compute_advance_minutes(self, sim_minute: int) -> int:
        tod = sim_minute % 1440
        if tod < 480:
            return 480 - tod
        if tod >= 1260:
            return (1440 - tod) + 480
        if 540 <= tod < 1140:
            return min(15, 1140 - tod)
        return min(30, 1260 - tod)

    def _apply_initial_day0_auto_configs(self, client: httpx.Client, sim_minute: int, ts: str) -> None:
        """At D0 00:00, apply default schedule.hour_weights and safe DID/canary setup where permitted."""
        for wid, wf_meta in self.workflows_meta.items():
            gov_auto = wf_meta["governance"].get("auto_apply", {})
            if not gov_auto.get("enabled", False):
                continue
            allowed_dims = set(gov_auto.get("allowed_dims", []))
            if "schedule" not in allowed_dims:
                continue

            cfg_resp = client.get(f"/workflows/{wid}/config").json()
            base_ver = int(cfg_resp["version"])
            cur_cfg = cfg_resp["config"]

            step_cfg = copy.deepcopy(cur_cfg)
            hw = self.target_hour_weights.get(wid, {})
            if "default" in hw:
                step_cfg["schedule"] = {
                    "hour_weights": {"default": hw["default"]},
                    "lead_preferred_hour": cur_cfg.get("schedule", {}).get("lead_preferred_hour", {}),
                }
            if "caller_id" in allowed_dims:
                acc = self.ingestor.get_workflow(wid)
                did_alloc = self.stats_engine.compute_did_allocation(
                    acc, wf_meta, cur_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
                )
                step_cfg.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = did_alloc

            allow_var_d0 = "welcome_message" in allowed_dims and "voice" in allowed_dims
            if allow_var_d0:
                cohorts = self.discovered_cohorts.get(wid, [])
                init_allocs = self.stats_engine.compute_cohort_allocations(
                    self.ingestor, wid, wf_meta, cohorts, cur_cfg.get("allocations", {})
                )
                for dim in ("welcome_message", "voice"):
                    if dim in init_allocs.get("default", {}):
                        step_cfg.setdefault("allocations", {}).setdefault("default", {})[dim] = init_allocs["default"][dim]

            verified_step = self.governance.build_auto_apply_step(
                wf_meta, cur_cfg, step_cfg, allow_variant_dims=allow_var_d0
            )
            if verified_step:
                self.governance.execute_auto_apply(
                    client=client,
                    ingestor=self.ingestor,
                    wf_meta=wf_meta,
                    base_version=base_ver,
                    cur_cfg=cur_cfg,
                    step_cfg=verified_step,
                    rationale="Bootstrap D0 call-time schedule hour_weights and safe DID/variant probe from history",
                    sim_minute=sim_minute,
                    ts=ts,
                )

    def _process_config_events(self, client: httpx.Client, sim_minute: int, ts: str) -> None:
        """Process `config_changed` events to detect and audit external supervisor edits (C9, C12)."""
        events = list(self.ingestor.pending_config_events)
        self.ingestor.pending_config_events.clear()

        for ev in events:
            wid = str(ev["workflow_id"])
            actor = str(ev.get("actor", ""))
            ver = int(ev.get("version", 1))
            dims = [str(d) for d in (ev.get("changed_dims") or ev.get("dims") or [])]
            ev_ts = str(ev.get("ts", ts))

            acc = self.ingestor.get_workflow(wid)
            acc.last_config_version = ver

            if actor == "supervisor":
                acc.last_supervisor_edit_minute = sim_minute
                acc.last_supervisor_edit_ts = ev_ts
                acc.last_supervisor_edit_dims = dims

                cfg_resp = client.get(f"/workflows/{wid}/config").json()
                live_cfg = cfg_resp["config"]
                new_pv = str(live_cfg.get("prompt_version", "P1"))
                if "prompt_version" in dims or new_pv != acc.active_prompt_version:
                    acc.reset_prompt_epoch(new_pv, sim_minute)

                wf_meta = self.workflows_meta[wid]
                inc = IncidentRecord(
                    incident_id=f"inc_sup_{wid}_v{ver}_{sim_minute}",
                    workflow_id=wid,
                    tenant_id=wf_meta["tenant_id"],
                    incident_type="supervisor_edit",
                    detected_at_minute=sim_minute,
                    detected_at_ts=ev_ts,
                    status="open" if "prompt_version" in dims else "observed",
                    details={
                        "version": ver,
                        "changed_dims": dims,
                        "prompt_version": new_pv,
                        "cohorts_count": len(live_cfg.get("cohorts", [])),
                    },
                )
                self.store.record_incident(inc)
                self.store.record_decision_audit(
                    decision_id=f"sup_{wid}_v{ver}_{sim_minute}",
                    sim_minute=sim_minute,
                    ts=ev_ts,
                    workflow_id=wid,
                    tenant_id=wf_meta["tenant_id"],
                    decision_type="supervisor_override",
                    actor="supervisor",
                    base_version=ver - 1,
                    new_version=ver,
                    proposal_id=None,
                    approval_id=None,
                    changed_dims=dims,
                    change_summary=f"External supervisor config edit on {wid} (v{ver}: {dims})",
                    rationale="External change by tenant supervisor; controller entering observation & safety monitoring",
                    diff={"changed_dims": dims, "new_prompt_version": new_pv},
                    dry_run={},
                    pre_kpis=self.governance._snapshot_kpis(acc),
                    verification_status="monitoring",
                )

    def _evaluate_workflow(
        self,
        client: httpx.Client,
        wid: str,
        wf_meta: dict[str, Any],
        sim_minute: int,
        ts: str,
    ) -> None:
        acc = self.ingestor.get_workflow(wid)
        cfg_resp = client.get(f"/workflows/{wid}/config").json()
        base_ver = int(cfg_resp["version"])
        cur_cfg = cfg_resp["config"]
        cur_prompt = str(cur_cfg.get("prompt_version", "P1"))

        if acc.active_prompt_version != cur_prompt:
            acc.reset_prompt_epoch(cur_prompt, sim_minute)

        # 1. Check post-apply verification if a prompt incident fix was recently applied (C8)
        self.incident_manager.check_post_apply_verification(acc, wid, cur_prompt, sim_minute, ts)

        # 2. Check for newly degraded DIDs (C5) or newly decayed variants (C7) and log incident records
        degraded_dids = self.stats_engine.detect_degraded_dids(
            acc, [str(d) for d in wf_meta["options"]["caller_id"]]
        )
        new_degraded = degraded_dids - self.reported_degraded_dids.get(wid, set())
        for d in sorted(new_degraded):
            win = acc.did_recent_window.get(d, [])
            rate = (sum(win) / len(win)) if win else 0.0
            self.store.record_incident(
                IncidentRecord(
                    incident_id=f"inc_did_{wid}_{d}_{sim_minute}",
                    workflow_id=wid,
                    tenant_id=wf_meta["tenant_id"],
                    incident_type="did_degraded",
                    detected_at_minute=sim_minute,
                    detected_at_ts=ts,
                    status="mitigating",
                    details={"caller_id": d, "recent_pickup_rate": round(rate, 4), "sample_dials": len(win)},
                )
            )
            self.reported_degraded_dids.setdefault(wid, set()).add(d)

        decayed_vars = self.stats_engine.detect_decaying_variants(acc, wf_meta)
        new_decayed = decayed_vars - self.reported_decayed_variants.get(wid, set())
        for dim, opt in sorted(new_decayed):
            win = acc.variant_recent_window.get((dim, opt), [])
            rate = (sum(win) / len(win)) if win else 0.0
            self.store.record_incident(
                IncidentRecord(
                    incident_id=f"inc_decay_{wid}_{dim}_{opt}_{sim_minute}",
                    workflow_id=wid,
                    tenant_id=wf_meta["tenant_id"],
                    incident_type="variant_decay",
                    detected_at_minute=sim_minute,
                    detected_at_ts=ts,
                    status="mitigating",
                    details={
                        "dimension": dim,
                        "option": opt,
                        "recent_engagement_rate": round(rate, 4),
                        "peak_engagement_rate": round(acc.variant_peak_rate.get((dim, opt), 0.0), 4),
                    },
                )
            )
            self.reported_decayed_variants.setdefault(wid, set()).add((dim, opt))

        # 3. Check for active hallucination spike on current prompt (C8)
        inc = self.incident_manager.check_hallucination_spike(acc, wf_meta, cur_prompt, sim_minute, ts)
        if inc and inc.status in ("open", "mitigating"):
            self._handle_urgent_prompt_incident(client, wid, wf_meta, base_ver, cur_cfg, inc, sim_minute, ts)
            return

        # 4. Build target desired configuration for this workflow
        cohorts = self.discovered_cohorts.get(wid, [])
        target_allocs = self.stats_engine.compute_cohort_allocations(
            self.ingestor, wid, wf_meta, cohorts, cur_cfg.get("allocations", {})
        )

        # Respect benign supervisor edits for `supervisor_edit_grace_minutes` unless an active DID/decay issue exists
        in_sup_grace = (
            acc.last_supervisor_edit_minute is not None
            and (sim_minute - acc.last_supervisor_edit_minute) < self.cfg.supervisor_edit_grace_minutes
            and not new_degraded
            and not new_decayed
        )
        if in_sup_grace:
            return

        # Preserve benign supervisor voice preference if competitive and non-decayed
        if acc.last_supervisor_edit_minute is not None and "voice" in acc.last_supervisor_edit_dims:
            sup_vc = cur_cfg.get("allocations", {}).get("default", {}).get("voice", {})
            if len(sup_vc) == 1:
                chosen_v = next(iter(sup_vc.keys()))
                if ("voice", chosen_v) not in acc.decayed_variants:
                    gv = acc.live_global_variants.get(("voice", chosen_v), [0, 0])
                    if gv[0] >= 50 and (gv[1] / gv[0]) >= 0.56:
                        target_allocs.setdefault("default", {})["voice"] = {chosen_v: 1.0}

        # Keep existing schedule if cohorts are already installed to avoid 0.01 rounding churn
        target_cohorts_dicts = [c.to_dict() for c in cohorts]
        if cur_cfg.get("cohorts") == target_cohorts_dicts and cur_cfg.get("schedule", {}).get("hour_weights"):
            sched_to_use = copy.deepcopy(cur_cfg["schedule"])
        else:
            sched_to_use = {
                "hour_weights": self.target_hour_weights.get(wid, {}),
                "lead_preferred_hour": cur_cfg.get("schedule", {}).get("lead_preferred_hour", {}),
            }

        target_cfg = {
            "cohorts": target_cohorts_dicts,
            "allocations": target_allocs,
            "prompt_version": cur_prompt,
            "schedule": sched_to_use,
        }

        # 5. If no pending approval is in flight, execute safe auto_apply for caller_id / schedule / decay rollback
        pending_for_wf = self.store.get_pending_proposals(wid)
        if not pending_for_wf:
            cur_did_dist = cur_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
            has_flagged_did_weight = any(cur_did_dist.get(d, 0.0) > 0.005 for d in degraded_dids)
            self._maybe_execute_auto_apply(
                client,
                wid,
                wf_meta,
                base_ver,
                cur_cfg,
                target_cfg,
                sim_minute,
                ts,
                has_urgent_did=has_flagged_did_weight,
                has_urgent_decay=bool(new_decayed),
            )
            cfg_resp = client.get(f"/workflows/{wid}/config").json()
            base_ver = int(cfg_resp["version"])
            cur_cfg = cfg_resp["config"]

        # 6. Build approval proposal (excluding caller_id if auto_apply already handles caller_id AND cur_cfg has no degraded DIDs)
        prop_target_cfg = copy.deepcopy(target_cfg)
        gov_auto = wf_meta["governance"].get("auto_apply", {})
        cur_did_alloc = cur_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
        has_degraded_in_cur = any(cur_did_alloc.get(d, 0.0) > 0 for d in acc.degraded_dids)
        if gov_auto.get("enabled", False) and "caller_id" in gov_auto.get("allowed_dims", []) and not has_degraded_in_cur:
            prop_target_cfg.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = copy.deepcopy(cur_did_alloc)

        has_urgent_rollback = bool(new_decayed) or (
            bool(new_degraded) and "caller_id" not in gov_auto.get("allowed_dims", [])
        )
        if has_urgent_rollback:
            self.governance.last_proposal_minute.pop(wid, None)

        if not self.governance.can_submit_proposal(wid, wf_meta, sim_minute, is_urgent=has_urgent_rollback):
            return

        changed_dims = compute_changed_dims(cur_cfg, prop_target_cfg)
        if not changed_dims:
            return

        auto_allowed = set(gov_auto.get("allowed_dims", []))
        can_auto_apply_all = (
            gov_auto.get("enabled", False)
            and set(changed_dims).issubset(auto_allowed)
            and not ("welcome_message" in changed_dims or "voice" in changed_dims)
        )
        if can_auto_apply_all:
            return

        max_alloc_tvd = self._max_allocation_tvd(cur_cfg, prop_target_cfg)
        if "cohorts" not in changed_dims and max_alloc_tvd < 0.12 and not has_urgent_rollback:
            return

        dry_run, affected_leads, impact_score = self.governance.build_dry_run_and_impact(
            self.ingestor, wid, wf_meta, cur_cfg, prop_target_cfg, changed_dims, is_urgent_incident=has_urgent_rollback
        )

        if not has_urgent_rollback:
            if dry_run["expected_engagement_delta"] < 0.001 or dry_run["expected_pickup_delta"] < -0.002:
                return
            if impact_score < self.cfg.min_impact_score_to_propose:
                return

        rationale = self._build_proposal_rationale(wid, wf_meta, cur_cfg, prop_target_cfg, changed_dims, dry_run)
        summary = self._build_proposal_summary(wid, changed_dims, has_urgent_rollback)
        proposal_id = f"prop_{wid}_v{base_ver}_m{sim_minute}_{uuid.uuid4().hex[:6]}"
        ttl_hours = int(wf_meta["governance"].get("proposal_ttl_hours", 24))

        pkg = ProposalPackage(
            proposal_id=proposal_id,
            workflow_id=wid,
            tenant_id=wf_meta["tenant_id"],
            base_version=base_ver,
            proposed_config=prop_target_cfg,
            changed_dims=changed_dims,
            change_summary=summary,
            diff=compute_config_diff(cur_cfg, prop_target_cfg),
            affected_leads=affected_leads,
            dry_run=dry_run,
            impact_score=impact_score,
            rationale=rationale,
            evidence=None,
            is_urgent_incident=has_urgent_rollback,
            expires_in_hours=ttl_hours,
        )
        self.governance.submit_approval_request(client, self.ingestor, pkg, sim_minute, ts)

    def _handle_urgent_prompt_incident(
        self,
        client: httpx.Client,
        wid: str,
        wf_meta: dict[str, Any],
        base_ver: int,
        cur_cfg: dict[str, Any],
        inc: IncidentRecord,
        sim_minute: int,
        ts: str,
    ) -> None:
        """Select, validate, and propose a grounded prompt fix for a hallucination spike (C8)."""
        pending = self.store.get_pending_proposals(wid)
        if pending:
            for row in pending:
                if int(row.get("is_urgent", 0)) == 1 and int(row["base_version"]) == base_ver:
                    inc.status = "mitigating"
                    inc.mitigation_proposal_id = str(row["proposal_id"])
                    self.store.record_incident(inc)
                    return

        if not self.governance.can_submit_proposal(wid, wf_meta, sim_minute, is_urgent=True):
            return

        # Refresh exact credits remaining from /sim/state before calling LLM / validation endpoints
        st = client.get("/sim/state").json()
        self.credits_remaining = float(st.get("credits_remaining", self.credits_remaining))

        fix_res = self.incident_manager.select_and_validate_prompt_fix(
            client, wf_meta, inc, self.credits_remaining
        )
        if not fix_res:
            return
        cand_pv, val_data = fix_res
        self.credits_remaining = float(val_data.get("credits_remaining", self.credits_remaining))

        proposed_cfg = copy.deepcopy(cur_cfg)
        proposed_cfg["prompt_version"] = cand_pv
        acc = self.ingestor.get_workflow(wid)
        if acc.degraded_dids:
            cur_did = proposed_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
            if any(cur_did.get(d, 0.0) > 0 for d in acc.degraded_dids):
                clean_did = self.stats_engine.compute_did_allocation(acc, wf_meta, cur_did)
                proposed_cfg.setdefault("allocations", {}).setdefault("default", {})["caller_id"] = clean_did

        changed_dims = compute_changed_dims(cur_cfg, proposed_cfg)
        dry_run, affected_leads, impact_score = self.governance.build_dry_run_and_impact(
            self.ingestor, wid, wf_meta, cur_cfg, proposed_cfg, changed_dims, is_urgent_incident=True
        )
        bad_pv = inc.details.get("bad_prompt_version", cur_cfg.get("prompt_version"))
        obs_rate = inc.details.get("observed_hallucination_rate", 0.0)
        z_val = inc.details.get("z_score", 0.0)

        rationale = (
            f"URGENT INCIDENT MITIGATION ({inc.incident_id}): Prompt {bad_pv} spiked hallucination rate "
            f"to {obs_rate:.1%} (z={z_val} vs baseline {inc.details.get('baseline_hallucination_rate', 0.015):.1%}). "
            f"Validated grounded prompt {cand_pv} ({inc.details.get('selected_fix_summary', '')}) via "
            f"validation_run_id={val_data['validation_run_id']} with pass_rate={val_data['pass_rate']:.3f} "
            f"(n={val_data['n']}) while preserving natural conversation flow and engagement."
        )
        proposal_id = f"incfix_{wid}_v{base_ver}_m{sim_minute}_{uuid.uuid4().hex[:6]}"
        pkg = ProposalPackage(
            proposal_id=proposal_id,
            workflow_id=wid,
            tenant_id=wf_meta["tenant_id"],
            base_version=base_ver,
            proposed_config=proposed_cfg,
            changed_dims=changed_dims,
            change_summary=f"Urgent prompt safety fix {bad_pv} -> {cand_pv} (validated pass_rate={val_data['pass_rate']:.3f})",
            diff=compute_config_diff(cur_cfg, proposed_cfg),
            affected_leads=affected_leads,
            dry_run=dry_run,
            impact_score=impact_score,
            rationale=rationale,
            evidence={
                "validation_run_id": val_data["validation_run_id"],
                "pass_rate": val_data["pass_rate"],
                "n": val_data["n"],
                "incident_id": inc.incident_id,
            },
            is_urgent_incident=True,
            expires_in_hours=int(wf_meta["governance"].get("proposal_ttl_hours", 12)),
        )
        aid = self.governance.submit_approval_request(client, self.ingestor, pkg, sim_minute, ts)
        if aid:
            inc.status = "verifying"
            inc.mitigation_proposal_id = proposal_id
            self.store.record_incident(inc)

    def _maybe_execute_auto_apply(
        self,
        client: httpx.Client,
        wid: str,
        wf_meta: dict[str, Any],
        base_ver: int,
        cur_cfg: dict[str, Any],
        target_cfg: dict[str, Any],
        sim_minute: int,
        ts: str,
        has_urgent_did: bool,
        has_urgent_decay: bool,
    ) -> None:
        gov_auto = wf_meta["governance"].get("auto_apply", {})
        if not gov_auto.get("enabled", False):
            return

        last_auto = self.governance.last_auto_apply_minute.get(wid)
        min_gap = 15 if (has_urgent_did or has_urgent_decay) else 360
        if last_auto is not None and (sim_minute - last_auto) < min_gap:
            return

        acc = self.ingestor.get_workflow(wid)
        allow_var_auto = has_urgent_decay

        step_cfg = self.governance.build_auto_apply_step(
            wf_meta, cur_cfg, target_cfg, allow_variant_dims=allow_var_auto
        )
        if not step_cfg:
            return

        changed_dims = compute_changed_dims(cur_cfg, step_cfg)
        if not changed_dims:
            return

        cur_did = cur_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
        new_did = step_cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
        did_tvd = total_variation_distance(cur_did, new_did)
        if changed_dims == ["caller_id"] and not has_urgent_did and did_tvd < 0.06:
            return

        dry_run, _, _ = self.governance.build_dry_run_and_impact(
            self.ingestor, wid, wf_meta, cur_cfg, step_cfg, changed_dims
        )
        if not has_urgent_did and dry_run["expected_pickup_delta"] < -0.002:
            return
        if ("welcome_message" in changed_dims or "voice" in changed_dims) and not has_urgent_decay:
            if dry_run["expected_engagement_delta"] < 0.002 or self._max_allocation_tvd(cur_cfg, step_cfg) < 0.10:
                return

        rationale_parts = []
        if "schedule" in changed_dims:
            rationale_parts.append("update cohort schedule.hour_weights to empirical pickup peaks")
        if "caller_id" in changed_dims:
            if acc.degraded_dids:
                rationale_parts.append(f"rotate traffic away from degraded caller_id {sorted(acc.degraded_dids)}")
            else:
                rationale_parts.append("rebalance caller_id allocation across verified healthy DIDs")
        if "welcome_message" in changed_dims or "voice" in changed_dims:
            if acc.decayed_variants:
                rationale_parts.append(f"auto-rollback decaying variant {sorted(acc.decayed_variants)}")
            else:
                rationale_parts.append("step cohort allocations toward Empirical Bayes winners")

        rationale = f"Policy-bounded auto-apply (TVD <= {gov_auto.get('max_shift')}): " + "; ".join(rationale_parts)
        self.governance.execute_auto_apply(
            client=client,
            ingestor=self.ingestor,
            wf_meta=wf_meta,
            base_version=base_ver,
            cur_cfg=cur_cfg,
            step_cfg=step_cfg,
            rationale=rationale,
            sim_minute=sim_minute,
            ts=ts,
        )

    def _max_allocation_tvd(self, cur_cfg: dict[str, Any], target_cfg: dict[str, Any]) -> float:
        max_tvd = 0.0
        cur_alloc = cur_cfg.get("allocations", {})
        tgt_alloc = target_cfg.get("allocations", {})
        all_cids = set(cur_alloc.keys()) | set(tgt_alloc.keys()) | {"default"}
        for cid in all_cids:
            for dim in ("welcome_message", "voice", "caller_id"):
                c_d = cur_alloc.get(cid, {}).get(dim) or cur_alloc.get("default", {}).get(dim, {})
                t_d = tgt_alloc.get(cid, {}).get(dim) or tgt_alloc.get("default", {}).get(dim, {})
                max_tvd = max(max_tvd, total_variation_distance(c_d, t_d))
        return max_tvd

    def _build_proposal_summary(self, wid: str, changed_dims: list[str], is_urgent_rollback: bool) -> str:
        if is_urgent_rollback:
            return f"Rollback decayed/degraded arm and rebalance {changed_dims} on {wid}"
        if "cohorts" in changed_dims:
            return f"Install shared cohorts, call-time schedule, and safe variant allocations on {wid}"
        return f"Promote Empirical Bayes winning variants ({', '.join(changed_dims)}) on {wid}"

    def _build_proposal_rationale(
        self,
        wid: str,
        wf_meta: dict[str, Any],
        cur_cfg: dict[str, Any],
        target_cfg: dict[str, Any],
        changed_dims: list[str],
        dry_run: dict[str, Any],
    ) -> str:
        acc = self.ingestor.get_workflow(wid)
        parts: list[str] = []
        if "cohorts" in changed_dims:
            c_ids = [c["id"] for c in target_cfg.get("cohorts", [])]
            parts.append(f"Segmented workflow into {len(c_ids)} statistically supported cohorts ({', '.join(c_ids)})")
        if "schedule" in changed_dims:
            parts.append("aligned per-cohort schedule.hour_weights with empirical hourly pickup curves")
        if acc.decayed_variants:
            parts.append(f"rolled back decayed variants {sorted(acc.decayed_variants)} to healthy runner-up")
        if acc.degraded_dids and "caller_id" in changed_dims:
            parts.append(f"shifted dials off degraded DIDs {sorted(acc.degraded_dids)}")
        if "welcome_message" in changed_dims or "voice" in changed_dims:
            parts.append("updated cohort variant allocations via Empirical Bayes + Benjamini-Hochberg FDR control")
        parts.append(
            f"Historical dry-run projects pickup {dry_run['baseline_pickup_rate']:.3f} -> {dry_run['projected_pickup_rate']:.3f} "
            f"({dry_run['expected_pickup_delta']:+.3f}) and engagement {dry_run['baseline_engagement_rate']:.3f} -> "
            f"{dry_run['projected_engagement_rate']:.3f} ({dry_run['expected_engagement_delta']:+.3f}); guardrails PASS."
        )
        return "; ".join(parts)

    def _update_post_decision_kpis(self, sim_minute: int) -> None:
        rows = self.store.conn.execute(
            """
            SELECT decision_id, workflow_id, sim_minute FROM decisions_audit
            WHERE verification_status = 'pending_observation'
              AND decision_type IN ('auto_apply', 'approval_applied')
              AND ? - sim_minute >= 360
            """,
            (sim_minute,),
        ).fetchall()
        for r in rows:
            wid = str(r["workflow_id"])
            acc = self.ingestor.get_workflow(wid)
            recent = list(acc.recent_live_calls)
            if len(recent) >= 50:
                conn = sum(x[0] for x in recent)
                eng = sum(x[1] for x in recent)
                hal = sum(x[2] for x in recent)
                post = {
                    "sample_dials": len(recent),
                    "pickup_rate": round(conn / max(1, len(recent)), 4),
                    "engagement_rate": round(eng / max(1, conn), 4),
                    "hallucination_rate": round(hal / max(1, conn), 4),
                }
                self.store.update_decision_post_kpis(str(r["decision_id"]), post, "verified")

    def _record_tick_telemetry(
        self,
        sim_minute: int,
        ts: str,
        wall_elapsed_ms: float,
        events_ingested: int,
        ingest_eps: float,
        credits_remaining: float,
    ) -> None:
        pending_cnt = len(self.store.get_pending_proposals())
        dec_row = self.store.conn.execute("SELECT COUNT(*) AS cnt FROM decisions_audit").fetchone()
        dec_cnt = int(dec_row["cnt"]) if dec_row else 0
        mem_mb = _get_memory_mb()

        alerts: list[dict[str, Any]] = []
        if wall_elapsed_ms > 5000.0:
            alerts.append({"alert": "high_tick_latency", "value_ms": round(wall_elapsed_ms, 1), "threshold_ms": 5000.0})
        if credits_remaining < 15.0:
            alerts.append({"alert": "low_validation_credits", "remaining": credits_remaining, "threshold": 15.0})
        for wid, inc in self.incident_manager.active_incidents.items():
            if inc.status in ("open", "mitigating"):
                alerts.append({"alert": "active_safety_incident", "workflow_id": wid, "incident_id": inc.incident_id})

        self.store.record_loop_metric(
            sim_minute=sim_minute,
            ts=ts,
            wall_elapsed_ms=round(wall_elapsed_ms, 2),
            events_ingested=events_ingested,
            ingest_eps=round(ingest_eps, 1),
            active_pending_approvals=pending_cnt,
            decisions_count=dec_cnt,
            credits_remaining=credits_remaining,
            memory_mb=round(mem_mb, 2),
            alerts=alerts,
        )
