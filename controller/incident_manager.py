"""Hallucination incident detection, LLM fix selection, validation, and post-apply verification (C8)."""
from typing import Any
import uuid
import httpx

from controller.config import ControllerConfig, DEFAULT_CONFIG
from controller.ingestor import WorkflowAccumulator
from controller.models import IncidentRecord
from controller.stats_engine import two_prop_z
from controller.store import AuditStore


class IncidentManager:
    """Manages the C8 incident lifecycle: detect -> LLM suggest -> validate -> propose -> verify."""

    def __init__(self, store: AuditStore, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.store = store
        self.cfg = cfg
        # workflow_id -> active IncidentRecord
        self.active_incidents: dict[str, IncidentRecord] = {}
        # Cache validated prompt runs: (workflow_id, prompt_version) -> validation_response_dict
        self.validated_prompts: dict[tuple[str, str], dict[str, Any]] = {}

    def check_hallucination_spike(
        self,
        acc: WorkflowAccumulator,
        wf_meta: dict[str, Any],
        current_prompt: str,
        sim_minute: int,
        ts: str,
    ) -> IncidentRecord | None:
        """Detect population-level hallucination spikes on the active prompt_version (C8)."""
        wid = wf_meta["workflow_id"]
        tenant_id = wf_meta["tenant_id"]

        # If there is already an open/mitigating incident for this prompt, return it
        existing = self.active_incidents.get(wid)
        if existing and existing.status in ("open", "mitigating"):
            if existing.details.get("bad_prompt_version") == current_prompt:
                return existing

        # Check recent window and epoch stats for current_prompt
        epoch = acc.prompt_epoch_stats.get(current_prompt, [0, 0, 0])
        recent_win = acc.prompt_recent_halluc.get(current_prompt)

        if recent_win and len(recent_win) >= self.cfg.halluc_min_connected_calls:
            n_cur = len(recent_win)
            h_cur = sum(recent_win)
        else:
            n_cur = epoch[0]
            h_cur = epoch[2]

        if n_cur < self.cfg.halluc_min_connected_calls:
            return None

        cur_rate = h_cur / n_cur
        hist_conn = max(1, acc.history_totals[1])
        hist_halluc = acc.history_totals[3]
        hist_rate = hist_halluc / hist_conn if acc.history_totals[1] > 0 else 0.015

        z_score = two_prop_z(h_cur, n_cur, hist_halluc, hist_conn)
        threshold_rate = max(self.cfg.halluc_min_abs_rate, hist_rate * self.cfg.halluc_rate_multiplier)

        if cur_rate >= threshold_rate and z_score >= self.cfg.halluc_z_threshold:
            inc = IncidentRecord(
                incident_id=f"inc_hal_{wid}_{sim_minute}_{uuid.uuid4().hex[:6]}",
                workflow_id=wid,
                tenant_id=tenant_id,
                incident_type="hallucination_spike",
                detected_at_minute=sim_minute,
                detected_at_ts=ts,
                status="open",
                details={
                    "bad_prompt_version": current_prompt,
                    "observed_hallucination_rate": round(cur_rate, 4),
                    "observed_hallucinations": h_cur,
                    "sample_connected_calls": n_cur,
                    "baseline_hallucination_rate": round(hist_rate, 4),
                    "z_score": round(z_score, 2),
                },
            )
            self.active_incidents[wid] = inc
            self.store.record_incident(inc)
            return inc

        return None

    def select_and_validate_prompt_fix(
        self,
        client: httpx.Client,
        wf_meta: dict[str, Any],
        inc: IncidentRecord,
        credits_remaining: float,
    ) -> tuple[str, dict[str, Any]] | None:
        """Call /llm/suggest-fixes, filter out engagement-killing verbatim scripts, and run /validation/runs."""
        wid = wf_meta["workflow_id"]
        bad_prompt = inc.details.get("bad_prompt_version", "")
        min_pass = float(wf_meta["governance"].get("min_validation_pass_rate", 0.97))
        allowed_options = [str(p) for p in wf_meta.get("options", {}).get("prompt_version", [])]
        catalog = wf_meta.get("prompt_catalog", {})

        # 1. Check if we already have a cached passing validation run for a safe prompt option
        for pv in allowed_options:
            if pv != bad_prompt and (wid, pv) in self.validated_prompts:
                val = self.validated_prompts[(wid, pv)]
                if float(val.get("pass_rate", 0.0)) >= min_pass:
                    return pv, val

        if credits_remaining < 6.0:
            return None

        # 2. Query LLM for suggested prompt fixes (3 credits)
        problem_desc = (
            f"Hallucination rate spiked to {inc.details.get('observed_hallucination_rate', 0):.1%} "
            f"on prompt {bad_prompt} (z={inc.details.get('z_score')}). Need safe grounded prompt."
        )
        resp = client.post("/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem_desc})
        if resp.status_code != 200:
            return None

        suggestions = resp.json().get("suggestions", [])
        candidates: list[tuple[int, str, str]] = []

        for item in suggestions:
            pv = str(item.get("prompt_version", ""))
            summary = str(item.get("summary", catalog.get(pv, "")))
            if not pv or pv == bad_prompt:
                continue
            summary_lower = summary.lower()
            is_allowed = pv in allowed_options
            is_strict_verbatim = "verbatim" in summary_lower or "strict:" in summary_lower
            is_grounded = "grounded" in summary_lower or "restricted" in summary_lower
            # Avoid persuasive prompts during a hallucination incident
            is_persuasive = "persuasive" in summary_lower or "freer" in summary_lower

            # Priority score (higher is better)
            score = 0
            if is_allowed:
                score += 10
            if is_grounded:
                score += 8
            if not is_strict_verbatim:
                score += 5
            if not is_persuasive:
                score += 4
            candidates.append((score, pv, summary))

        # Ensure fallback options from workflow.options.prompt_version are also considered
        seen_pvs = {pv for _, pv, _ in candidates}
        for pv in allowed_options:
            if pv != bad_prompt and pv not in seen_pvs:
                summary = str(catalog.get(pv, ""))
                s_low = summary.lower()
                score = 10 + (8 if "grounded" in s_low else 0) + (5 if "verbatim" not in s_low else 0)
                candidates.append((score, pv, summary))

        candidates.sort(key=lambda x: x[0], reverse=True)

        # 3. Validate candidates in priority order using /validation/runs
        for _score, cand_pv, cand_summary in candidates:
            val_resp = client.post(
                "/validation/runs",
                json={
                    "workflow_id": wid,
                    "prompt_version": cand_pv,
                    "n": self.cfg.validation_sample_size,
                },
            )
            if val_resp.status_code != 200:
                continue
            val_data = val_resp.json()
            if float(val_data.get("pass_rate", 0.0)) >= min_pass:
                self.validated_prompts[(wid, cand_pv)] = val_data
                inc.details["selected_fix_prompt"] = cand_pv
                inc.details["selected_fix_summary"] = cand_summary
                inc.details["validation_run_id"] = val_data["validation_run_id"]
                inc.details["validation_pass_rate"] = val_data["pass_rate"]
                self.store.record_incident(inc)
                return cand_pv, val_data

        return None

    def check_post_apply_verification(
        self,
        acc: WorkflowAccumulator,
        wid: str,
        current_prompt: str,
        sim_minute: int,
        ts: str,
    ) -> bool:
        """Verify after applying a prompt fix that hallucination rate returned to safe baseline (C8)."""
        inc = self.active_incidents.get(wid)
        if not inc or inc.status != "verifying":
            return False

        target_prompt = inc.details.get("selected_fix_prompt")
        if target_prompt and current_prompt != target_prompt:
            return False

        stats = acc.prompt_epoch_stats.get(current_prompt, [0, 0, 0])
        conn, eng, halluc = stats[0], stats[1], stats[2]
        if conn < self.cfg.post_fix_verify_calls:
            return False

        post_halluc_rate = halluc / conn
        post_eng_rate = eng / conn
        inc.status = "resolved"
        inc.resolved_at_minute = sim_minute
        inc.resolved_at_ts = ts
        inc.verification_summary = (
            f"Verified on n={conn} connected calls for {current_prompt}: "
            f"hallucination_rate={post_halluc_rate:.3f}, engagement_rate={post_eng_rate:.3f}"
        )
        inc.details["post_fix_hallucination_rate"] = round(post_halluc_rate, 4)
        inc.details["post_fix_engagement_rate"] = round(post_eng_rate, 4)
        inc.details["post_fix_sample_calls"] = conn
        self.store.record_incident(inc)
        return True
