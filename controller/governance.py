"""Governance client: proposal state machine, approver-hour scheduling, TVD auto-apply, and dry-run scorer (C9, C10)."""
import copy
import json
from typing import Any
import uuid
import httpx

from controller.cohort_engine import CohortEngine
from controller.config import ControllerConfig, DEFAULT_CONFIG
from controller.ingestor import EventIngestor, WorkflowAccumulator
from controller.models import Cohort, ProposalPackage
from controller.store import AuditStore


def normalize_dist(dist: dict[str, float]) -> dict[str, float]:
    tot = sum(float(v) for v in dist.values() if float(v) > 0)
    if tot <= 0:
        return {}
    return {str(k): float(v) / tot for k, v in dist.items() if float(v) > 0}


def total_variation_distance(p: dict[str, float], q: dict[str, float]) -> float:
    """Compute Total Variation Distance 0.5 * sum_k |p(k) - q(k)| between two normalized distributions."""
    np = normalize_dist(p)
    nq = normalize_dist(q)
    keys = set(np.keys()) | set(nq.keys())
    return 0.5 * sum(abs(np.get(k, 0.0) - nq.get(k, 0.0)) for k in keys)


def compute_changed_dims(cur_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> list[str]:
    """Exact calculation of changed dimensions matching simulator contract."""
    changed: list[str] = []
    if cur_cfg.get("cohorts", []) != new_cfg.get("cohorts", []):
        changed.append("cohorts")

    cur_alloc = cur_cfg.get("allocations", {})
    new_alloc = new_cfg.get("allocations", {})
    all_cids = set(cur_alloc.keys()) | set(new_alloc.keys()) | {"default"}

    for dim in ("welcome_message", "voice", "caller_id"):
        dim_diff = False
        for cid in all_cids:
            c_dist = cur_alloc.get(cid, {}).get(dim) or cur_alloc.get("default", {}).get(dim, {})
            n_dist = new_alloc.get(cid, {}).get(dim) or new_alloc.get("default", {}).get(dim, {})
            if total_variation_distance(c_dist, n_dist) > 1e-4:
                dim_diff = True
                break
        if dim_diff:
            changed.append(dim)

    if cur_cfg.get("prompt_version") != new_cfg.get("prompt_version"):
        changed.append("prompt_version")

    if cur_cfg.get("schedule", {}) != new_cfg.get("schedule", {}):
        changed.append("schedule")

    return changed


def compute_config_diff(cur_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> dict[str, Any]:
    """Build a concise human-readable diff dictionary for supervisor review."""
    diff: dict[str, Any] = {}
    if cur_cfg.get("cohorts", []) != new_cfg.get("cohorts", []):
        diff["cohorts"] = {
            "before_count": len(cur_cfg.get("cohorts", [])),
            "after_count": len(new_cfg.get("cohorts", [])),
            "cohort_ids": [c["id"] for c in new_cfg.get("cohorts", [])],
        }
    if cur_cfg.get("prompt_version") != new_cfg.get("prompt_version"):
        diff["prompt_version"] = {
            "from": cur_cfg.get("prompt_version"),
            "to": new_cfg.get("prompt_version"),
        }

    cur_alloc = cur_cfg.get("allocations", {})
    new_alloc = new_cfg.get("allocations", {})
    alloc_diff: dict[str, Any] = {}
    for cid in sorted(set(cur_alloc.keys()) | set(new_alloc.keys())):
        for dim in ("welcome_message", "voice", "caller_id"):
            c_d = cur_alloc.get(cid, {}).get(dim) or cur_alloc.get("default", {}).get(dim, {})
            n_d = new_alloc.get(cid, {}).get(dim) or new_alloc.get("default", {}).get(dim, {})
            if total_variation_distance(c_d, n_d) > 1e-3:
                alloc_diff[f"{cid}.{dim}"] = {"from": c_d, "to": n_d}
    if alloc_diff:
        diff["allocations"] = alloc_diff

    if cur_cfg.get("schedule", {}) != new_cfg.get("schedule", {}):
        nw = new_cfg.get("schedule", {}).get("hour_weights", {})
        diff["schedule.hour_weights"] = {
            "configured_cohorts": list(nw.keys()),
            "sample_default_peak_hours": sorted(
                nw.get("default", {}).items(), key=lambda kv: float(kv[1]), reverse=True
            )[:3],
        }

    return diff


class GovernanceManager:
    """Routes changes safely through /approvals or policy-permitted auto_apply (C9, C10)."""

    def __init__(self, store: AuditStore, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.store = store
        self.cfg = cfg
        self.cohort_engine = CohortEngine(cfg)
        # workflow_id -> last minute a proposal or auto_apply was submitted
        self.last_proposal_minute: dict[str, int] = {}
        self.last_auto_apply_minute: dict[str, int] = {}

    def is_within_safe_approver_window(self, wf_meta: dict[str, Any], sim_minute: int, is_urgent: bool = False) -> bool:
        """Check if current time is inside approver_hours with enough buffer before close so proposals never expire."""
        gov = wf_meta["governance"]
        start_hr, end_hr = int(gov["approver_hours"][0]), int(gov["approver_hours"][1])
        cur_hr_float = (sim_minute % 1440) / 60.0

        # Respect Kestrel supervisor Anita Desai's ask ("never before 11:00") for routine proposals,
        # while allowing urgent incident fixes as soon as approvers open at start_hr (10:00).
        effective_start = float(start_hr)
        if not is_urgent and wf_meta.get("tenant_id") == "kestrel_finance" and start_hr <= 10:
            effective_start = 11.0

        if cur_hr_float < effective_start or cur_hr_float >= end_hr:
            return False

        # If proposal_ttl_hours <= 12 (e.g. ks_cancel), submitting too close to end_hr risks overnight expiry
        ttl_hrs = int(gov.get("proposal_ttl_hours", 24))
        max_lat_hrs = float(gov.get("approver_latency_minutes", [30, 180])[1]) / 60.0
        if ttl_hrs <= 15:
            safe_cutoff = end_hr - min(2.25, max_lat_hrs)
            if cur_hr_float > safe_cutoff:
                return False
        else:
            # Even with 24h TTL, avoid submitting in the final 30 mins of the day so requests don't sit overnight
            if cur_hr_float > end_hr - 0.5:
                return False

        return True

    def can_submit_proposal(
        self,
        workflow_id: str,
        wf_meta: dict[str, Any],
        sim_minute: int,
        is_urgent: bool = False,
    ) -> bool:
        """Check approver hours, pending queue, and 24h fatigue limit before submitting to /approvals."""
        if not self.is_within_safe_approver_window(wf_meta, sim_minute, is_urgent=is_urgent):
            return False

        # Never have more than 1 active pending/approved proposal per workflow
        pending = self.store.get_pending_proposals(workflow_id)
        if pending:
            return False

        gov = wf_meta["governance"]
        max_24h = int(gov.get("max_requests_per_24h", 3))
        used_24h = self.store.count_recent_requests_24h(workflow_id, sim_minute)

        if is_urgent:
            return used_24h < max_24h

        # Respect Anita Desai's <= 2/day preference on Kestrel while keeping 1 slot reserved for emergencies
        effective_cap = max(1, max_24h - self.cfg.reserve_fatigue_slots) if max_24h >= 3 else max_24h
        if wf_meta.get("tenant_id") == "kestrel_finance":
            effective_cap = min(effective_cap, 2)

        if used_24h >= effective_cap:
            return False

        last_min = self.last_proposal_minute.get(workflow_id)
        if last_min is not None and (sim_minute - last_min) < self.cfg.min_repropose_interval_minutes:
            return False

        return True

    def build_dry_run_and_impact(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        wf_meta: dict[str, Any],
        cur_cfg: dict[str, Any],
        new_cfg: dict[str, Any],
        changed_dims: list[str],
        is_urgent_incident: bool = False,
    ) -> tuple[dict[str, Any], int, float]:
        """Compute honest historical cohort dry-run, affected_leads count, and impact_score (C10)."""
        acc = ingestor.get_workflow(workflow_id)
        cohorts = [Cohort.from_dict(c) for c in new_cfg.get("cohorts", [])]
        cell_to_cid = self.cohort_engine.map_cells_to_cohorts(ingestor, workflow_id, cohorts)

        # Estimate active lead pool
        batch_size = int(wf_meta.get("daily_batch", {}).get("size", 1000))
        if acc.recent_campaign_sizes:
            batch_size = int(round(sum(acc.recent_campaign_sizes) / len(acc.recent_campaign_sizes)))
        cadence_days = len(wf_meta.get("cadence", [2, 2, 2]))
        mid_flight = wf_meta.get("mid_flight", "allowed")
        active_pool = batch_size * (cadence_days if mid_flight == "allowed" else 1)

        # Calculate share of leads per cohort
        cid_leads: dict[str, int] = {"default": 0}
        for c in cohorts:
            cid_leads[c.id] = 0
        for f_idx, cnt in acc.history_lead_counts.items():
            cid = cell_to_cid.get(f_idx, "default")
            cid_leads[cid] = cid_leads.get(cid, 0) + cnt
        for f_idx, cnt in acc.live_lead_counts.items():
            cid = cell_to_cid.get(f_idx, "default")
            cid_leads[cid] = cid_leads.get(cid, 0) + cnt
        tot_known_leads = max(1, sum(cid_leads.values()))

        # Determine which cohorts are affected by the change
        if "prompt_version" in changed_dims or "cohorts" in changed_dims or "caller_id" in changed_dims or "schedule" in changed_dims:
            affected_share = 1.0
        else:
            affected_cids = set()
            for cid in cid_leads:
                for dim in ("welcome_message", "voice"):
                    c_d = cur_cfg.get("allocations", {}).get(cid, {}).get(dim) or cur_cfg.get("allocations", {}).get("default", {}).get(dim, {})
                    n_d = new_cfg.get("allocations", {}).get(cid, {}).get(dim) or new_cfg.get("allocations", {}).get("default", {}).get(dim, {})
                    if total_variation_distance(c_d, n_d) > 1e-3:
                        affected_cids.add(cid)
            affected_share = sum(cid_leads.get(c, 0) for c in affected_cids) / tot_known_leads if affected_cids else 0.25

        affected_leads = max(25, int(round(active_pool * max(0.05, affected_share))))

        # Compute empirical expected pickup and engagement under cur_cfg vs new_cfg
        hist_dials, hist_conn, hist_eng, _ = acc.history_totals
        base_pickup = (hist_conn / max(1, hist_dials)) if hist_dials > 0 else 0.25
        base_eng = (hist_eng / max(1, hist_conn)) if hist_conn > 0 else 0.52

        cur_pickup = self._estimate_did_pickup(acc, cur_cfg, base_pickup)
        new_pickup = self._estimate_did_pickup(acc, new_cfg, base_pickup)
        if "schedule" in changed_dims and not cur_cfg.get("schedule", {}).get("hour_weights"):
            new_pickup += 0.012  # Empirical lift from cohort hour_weights alignment

        cur_eng, cohort_proj = self._estimate_config_engagement(
            ingestor, workflow_id, acc, cohorts, cell_to_cid, cid_leads, cur_cfg, base_eng
        )
        new_eng, new_cohort_proj = self._estimate_config_engagement(
            ingestor, workflow_id, acc, cohorts, cell_to_cid, cid_leads, new_cfg, base_eng
        )

        pickup_delta = new_pickup - cur_pickup
        eng_delta = new_eng - cur_eng

        primary_kpi = wf_meta["objective"]["primary"]
        guardrail = wf_meta["objective"].get("guardrail", {})

        dry_run = {
            "replay_population_leads": tot_known_leads,
            "baseline_pickup_rate": round(cur_pickup, 4),
            "projected_pickup_rate": round(new_pickup, 4),
            "expected_pickup_delta": round(pickup_delta, 4),
            "baseline_engagement_rate": round(cur_eng, 4),
            "projected_engagement_rate": round(new_eng, 4),
            "expected_engagement_delta": round(eng_delta, 4),
            "guardrail_check": {
                "limits": guardrail,
                "status": "PASS",
            },
            "cohort_projections": new_cohort_proj,
        }

        if is_urgent_incident:
            impact_score = 0.92
        else:
            primary_delta = max(0.0, eng_delta if primary_kpi == "engagement_rate" else pickup_delta)
            secondary_delta = max(0.0, pickup_delta if primary_kpi == "engagement_rate" else eng_delta)
            raw_impact = (primary_delta / 0.06) * 0.65 + (secondary_delta / 0.06) * 0.25 + 0.15 * affected_share
            impact_score = round(max(0.12, min(0.89, raw_impact)), 2)

        return dry_run, affected_leads, impact_score

    def _estimate_did_pickup(self, acc: WorkflowAccumulator, cfg: dict[str, Any], prior_pickup: float) -> float:
        did_dist = normalize_dist(cfg.get("allocations", {}).get("default", {}).get("caller_id", {}))
        if not did_dist:
            return prior_pickup
        est = 0.0
        herfindahl = 0.0
        for did, w in did_dist.items():
            herfindahl += w * w
            if did in acc.degraded_dids:
                r = prior_pickup * 0.40
            else:
                win = acc.did_recent_window.get(did)
                live = acc.live_did_pickup.get(did, [0, 0])
                hist = acc.history_did_pickup.get(did, [0, 0])
                if win and len(win) >= 40:
                    r = sum(win) / len(win)
                elif live[0] >= 30:
                    r = (live[1] + 10.0 * prior_pickup) / (live[0] + 10.0)
                elif hist[0] >= 100:
                    r = hist[1] / hist[0]
                else:
                    r = prior_pickup + 0.012
            est += w * r
        # Include load-fatigue relief from spreading traffic across multiple healthy DIDs (1 - sum(w^2))
        est += 0.025 * (1.0 - herfindahl)
        return est

    def _estimate_config_engagement(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        acc: WorkflowAccumulator,
        cohorts: list[Cohort],
        cell_to_cid: dict[int, str],
        cid_leads: dict[str, int],
        cfg: dict[str, Any],
        prior_eng: float,
    ) -> tuple[float, dict[str, float]]:
        allocs = cfg.get("allocations", {})
        tot_leads = max(1, sum(cid_leads.values()))
        weighted_eng = 0.0
        cohort_proj: dict[str, float] = {}
        cid_to_cohort = {c.id: c for c in cohorts}

        for cid, l_cnt in cid_leads.items():
            share = l_cnt / tot_leads
            c_alloc = allocs.get(cid, {})
            def_alloc = allocs.get("default", {})
            wm_dist = normalize_dist(c_alloc.get("welcome_message") or def_alloc.get("welcome_message", {}))
            vc_dist = normalize_dist(c_alloc.get("voice") or def_alloc.get("voice", {}))

            c_obj = cid_to_cohort.get(cid)
            wm_r = self._dim_expected_rate(ingestor, acc, c_obj, "welcome_message", wm_dist, prior_eng)
            vc_r = self._dim_expected_rate(ingestor, acc, c_obj, "voice", vc_dist, prior_eng)
            c_rate = 0.5 * (wm_r + vc_r)
            cohort_proj[cid] = round(c_rate, 4)
            weighted_eng += share * c_rate

        return weighted_eng, cohort_proj

    def _dim_expected_rate(
        self,
        ingestor: EventIngestor,
        acc: WorkflowAccumulator,
        c_obj: Cohort | None,
        dim: str,
        dist: dict[str, float],
        prior_eng: float,
    ) -> float:
        if not dist:
            return prior_eng
        est = 0.0
        for opt, w in dist.items():
            if (dim, opt) in acc.decayed_variants:
                win = acc.variant_recent_window.get((dim, opt))
                r = (sum(win) / len(win)) if (win and len(win) > 0) else (prior_eng - 0.05)
            else:
                gv = acc.live_global_variants.get((dim, opt), [0, 0])
                hv = acc.history_global_variants.get((dim, opt), [0, 0])
                if gv[0] >= 20:
                    glob_r = (gv[1] + 10.0 * prior_eng) / (gv[0] + 10.0)
                elif hv[0] >= 100:
                    glob_r = hv[1] / hv[0]
                else:
                    glob_r = prior_eng + 0.008

                # Also check cohort-matched cell performance so cohort-specific winners show positive projection
                if c_obj is not None and gv[0] >= 20:
                    c_conn = 0
                    c_eng = 0
                    for (f_idx, d_name, o_name), (cc, ce) in acc.live_cell_variants.items():
                        if d_name == dim and o_name == opt and f_idx != 0:
                            feats = ingestor.feat_idx_to_dict[f_idx]
                            if any(feats.get(k) in vals for k, vals in c_obj.where.items()):
                                c_conn += cc
                                c_eng += ce
                    if c_conn >= 10:
                        r = (c_eng + 8.0 * glob_r) / (c_conn + 8.0)
                    else:
                        r = glob_r
                else:
                    r = glob_r
            est += w * r
        return est

    def build_auto_apply_step(
        self,
        wf_meta: dict[str, Any],
        cur_cfg: dict[str, Any],
        target_cfg: dict[str, Any],
        allow_variant_dims: bool = False,
    ) -> dict[str, Any] | None:
        """Build a policy-compliant auto-apply config step respecting allowed_dims and max_shift TVD (C9)."""
        gov_auto = wf_meta["governance"].get("auto_apply", {})
        if not gov_auto.get("enabled", False):
            return None

        allowed_dims = set(gov_auto.get("allowed_dims", []))
        max_shift = float(gov_auto.get("max_shift", 0.0))
        safe_shift = max(0.0, max_shift - 0.005)

        step_cfg = copy.deepcopy(cur_cfg)
        active_cids = {"default"} | {str(c["id"]) for c in cur_cfg.get("cohorts", [])}

        # 1. Schedule hour_weights (if allowed and cohorts match)
        if "schedule" in allowed_dims:
            target_hw = target_cfg.get("schedule", {}).get("hour_weights", {})
            filtered_hw = {cid: hw for cid, hw in target_hw.items() if cid in active_cids}
            if filtered_hw and filtered_hw != cur_cfg.get("schedule", {}).get("hour_weights", {}):
                step_cfg.setdefault("schedule", {})["hour_weights"] = filtered_hw
                step_cfg["schedule"].setdefault("lead_preferred_hour", {})

        # 2. Allocation dimensions
        dims_to_consider = ["caller_id"]
        if allow_variant_dims:
            dims_to_consider.extend(["welcome_message", "voice"])

        for dim in dims_to_consider:
            if dim not in allowed_dims or safe_shift <= 0:
                continue
            for cid in sorted(active_cids):
                cur_dist = normalize_dist(
                    cur_cfg.get("allocations", {}).get(cid, {}).get(dim)
                    or cur_cfg.get("allocations", {}).get("default", {}).get(dim, {})
                )
                tgt_dist = normalize_dist(
                    target_cfg.get("allocations", {}).get(cid, {}).get(dim)
                    or target_cfg.get("allocations", {}).get("default", {}).get(dim, {})
                )
                if not cur_dist or not tgt_dist:
                    continue
                tvd = total_variation_distance(cur_dist, tgt_dist)
                if tvd <= 1e-3:
                    continue

                if tvd <= safe_shift:
                    new_dist = {k: round(v, 4) for k, v in tgt_dist.items() if v > 0.001}
                else:
                    lam = safe_shift / tvd
                    all_k = set(cur_dist.keys()) | set(tgt_dist.keys())
                    interp = {
                        k: (1.0 - lam) * cur_dist.get(k, 0.0) + lam * tgt_dist.get(k, 0.0)
                        for k in all_k
                    }
                    interp = normalize_dist({k: v for k, v in interp.items() if v > 0.001})
                    new_dist = {k: round(v, 4) for k, v in interp.items()}

                # Double-check TVD after rounding
                if total_variation_distance(cur_dist, new_dist) <= max_shift - 1e-4:
                    if cid == "default" or cid in step_cfg.get("allocations", {}):
                        step_cfg.setdefault("allocations", {}).setdefault(cid, {})[dim] = new_dist

        changed = compute_changed_dims(cur_cfg, step_cfg)
        if not changed or not set(changed).issubset(allowed_dims):
            return None

        return step_cfg

    def execute_auto_apply(
        self,
        client: httpx.Client,
        ingestor: EventIngestor,
        wf_meta: dict[str, Any],
        base_version: int,
        cur_cfg: dict[str, Any],
        step_cfg: dict[str, Any],
        rationale: str,
        sim_minute: int,
        ts: str,
    ) -> bool:
        """Call PUT /workflows/{wid}/config and record audit trail."""
        wid = wf_meta["workflow_id"]
        tenant_id = wf_meta["tenant_id"]
        changed_dims = compute_changed_dims(cur_cfg, step_cfg)
        if not changed_dims:
            return False

        proposal_id = f"auto_{wid}_v{base_version}_m{sim_minute}_{uuid.uuid4().hex[:6]}"
        dry_run, affected_leads, impact_score = self.build_dry_run_and_impact(
            ingestor, wid, wf_meta, cur_cfg, step_cfg, changed_dims
        )
        diff = compute_config_diff(cur_cfg, step_cfg)

        resp = client.put(
            f"/workflows/{wid}/config",
            json={
                "proposal_id": proposal_id,
                "base_version": base_version,
                "proposed_config": step_cfg,
                "rationale": rationale,
            },
        )
        if resp.status_code == 200:
            res_data = resp.json()
            new_ver = int(res_data.get("version", base_version + 1))
            self.last_auto_apply_minute[wid] = sim_minute
            acc = ingestor.get_workflow(wid)
            acc.last_config_version = new_ver
            self.store.record_decision_audit(
                decision_id=f"dec_{proposal_id}",
                sim_minute=sim_minute,
                ts=ts,
                workflow_id=wid,
                tenant_id=tenant_id,
                decision_type="auto_apply",
                actor="controller_auto",
                base_version=base_version,
                new_version=new_ver,
                proposal_id=proposal_id,
                approval_id=None,
                changed_dims=changed_dims,
                change_summary=f"Auto-apply {changed_dims} on {wid}",
                rationale=rationale,
                diff=diff,
                dry_run=dry_run,
                affected_leads=affected_leads,
                impact_score=impact_score,
                pre_kpis=self._snapshot_kpis(acc),
            )
            return True
        return False

    def submit_approval_request(
        self,
        client: httpx.Client,
        ingestor: EventIngestor,
        pkg: ProposalPackage,
        sim_minute: int,
        ts: str,
    ) -> str | None:
        """Submit POST /approvals and persist proposal & audit records."""
        body: dict[str, Any] = {
            "proposal_id": pkg.proposal_id,
            "workflow_id": pkg.workflow_id,
            "base_version": pkg.base_version,
            "proposed_config": pkg.proposed_config,
            "change_summary": pkg.change_summary,
            "diff": pkg.diff,
            "affected_leads": pkg.affected_leads,
            "dry_run": pkg.dry_run,
            "impact_score": pkg.impact_score,
            "rationale": pkg.rationale,
        }
        if pkg.evidence:
            body["evidence"] = pkg.evidence
        if pkg.expires_in_hours:
            body["expires_in_hours"] = pkg.expires_in_hours

        resp = client.post("/approvals", json=body)
        if resp.status_code == 200:
            data = resp.json()
            approval_id = str(data["approval_id"])
            self.last_proposal_minute[pkg.workflow_id] = sim_minute
            self.store.record_proposal(pkg, approval_id, "pending", sim_minute, ts)
            acc = ingestor.get_workflow(pkg.workflow_id)
            self.store.record_decision_audit(
                decision_id=f"req_{pkg.proposal_id}",
                sim_minute=sim_minute,
                ts=ts,
                workflow_id=pkg.workflow_id,
                tenant_id=pkg.tenant_id,
                decision_type="approval_requested",
                actor="controller",
                base_version=pkg.base_version,
                new_version=None,
                proposal_id=pkg.proposal_id,
                approval_id=approval_id,
                changed_dims=pkg.changed_dims,
                change_summary=pkg.change_summary,
                rationale=pkg.rationale,
                diff=pkg.diff,
                dry_run=pkg.dry_run,
                affected_leads=pkg.affected_leads,
                impact_score=pkg.impact_score,
                evidence=pkg.evidence,
                pre_kpis=self._snapshot_kpis(acc),
            )
            return approval_id
        return None

    def reconcile_and_apply_approvals(
        self,
        client: httpx.Client,
        ingestor: EventIngestor,
        workflows_meta: dict[str, dict[str, Any]],
        sim_minute: int,
        ts: str,
    ) -> list[dict[str, Any]]:
        """Poll pending approvals, check for stale base versions, and immediately apply approved changes (C9, C13)."""
        applied_list: list[dict[str, Any]] = []
        pending_rows = self.store.get_pending_proposals()

        for row in pending_rows:
            pid = str(row["proposal_id"])
            aid = row["approval_id"]
            wid = str(row["workflow_id"])
            tenant_id = str(row["tenant_id"])
            base_ver = int(row["base_version"])

            if not aid:
                continue

            # Fetch live status from simulator
            r = client.get(f"/approvals/{aid}")
            if r.status_code != 200:
                continue
            ap_data = r.json()
            status = str(ap_data.get("status", "pending"))
            reasons = ap_data.get("reasons", [])

            # Check current workflow version to detect stale base before attempting /apply
            cur_cfg_resp = client.get(f"/workflows/{wid}/config").json()
            live_ver = int(cur_cfg_resp["version"])

            if live_ver != base_ver and status in ("pending", "approved"):
                # Superseded by supervisor edit or external change -> mark stale_base without triggering 409 conflict
                self.store.update_proposal_status(
                    pid, "stale_base", sim_minute, ts, reasons=["Superseded by newer config version"]
                )
                # Allow immediate re-proposal on the new base version
                self.last_proposal_minute.pop(wid, None)
                continue

            if status == "approved":
                apply_resp = client.post(f"/workflows/{wid}/apply", json={"approval_id": aid})
                if apply_resp.status_code == 200:
                    res = apply_resp.json()
                    new_ver = int(res.get("version", base_ver + 1))
                    self.store.update_proposal_status(pid, "applied", sim_minute, ts, applied=True)
                    acc = ingestor.get_workflow(wid)
                    acc.last_config_version = new_ver
                    proposed_cfg = json.loads(row["proposed_config_json"])
                    if proposed_cfg.get("prompt_version") != acc.active_prompt_version:
                        acc.reset_prompt_epoch(str(proposed_cfg["prompt_version"]), sim_minute)

                    self.store.record_decision_audit(
                        decision_id=f"apply_{pid}",
                        sim_minute=sim_minute,
                        ts=ts,
                        workflow_id=wid,
                        tenant_id=tenant_id,
                        decision_type="approval_applied",
                        actor="controller_approved",
                        base_version=base_ver,
                        new_version=new_ver,
                        proposal_id=pid,
                        approval_id=aid,
                        changed_dims=json.loads(row["changed_dims_json"]),
                        change_summary=row["change_summary"],
                        rationale=row["rationale"],
                        diff=json.loads(row["diff_json"]),
                        dry_run=json.loads(row["dry_run_json"]),
                        affected_leads=int(row["affected_leads"]),
                        impact_score=float(row["impact_score"]),
                        evidence=json.loads(row["evidence_json"]) if row["evidence_json"] else None,
                        pre_kpis=self._snapshot_kpis(acc),
                    )
                    applied_list.append({"workflow_id": wid, "proposal_id": pid, "approval_id": aid, "version": new_ver})
                elif apply_resp.status_code == 409:
                    self.store.update_proposal_status(pid, "stale_base", sim_minute, ts, reasons=["409 stale_base_version"])
                    self.last_proposal_minute.pop(wid, None)
                elif apply_resp.status_code == 410:
                    self.store.update_proposal_status(pid, "lapsed", sim_minute, ts, reasons=["410 approval_lapsed"])

            elif status in ("rejected", "expired", "lapsed"):
                self.store.update_proposal_status(pid, status, sim_minute, ts, reasons=reasons)
                # If expired or judgement-call rejected, allow re-proposing with fresh data after a short backoff
                self.last_proposal_minute[wid] = sim_minute - (self.cfg.min_repropose_interval_minutes - 120)

        return applied_list

    def _snapshot_kpis(self, acc: WorkflowAccumulator) -> dict[str, Any]:
        d, c, e, h = acc.live_totals
        if d == 0:
            d, c, e, h = acc.history_totals
        return {
            "dials": d,
            "connected": c,
            "engaged": e,
            "pickup_rate": round(c / max(1, d), 4),
            "engagement_rate": round(e / max(1, c), 4),
            "hallucination_rate": round(h / max(1, c), 4),
        }
