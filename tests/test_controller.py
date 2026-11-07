"""Unit tests for the Outcome-First Controller (cohorts, stats, governance, incidents, audit CLI)."""
from collections import deque
from pathlib import Path

from controller.cli import cmd_explain, cmd_health, cmd_supervisor_view
from controller.config import ControllerConfig
from controller.governance import (
    GovernanceManager,
    compute_changed_dims,
    total_variation_distance,
)
from controller.incident_manager import IncidentManager
from controller.ingestor import EventIngestor
from controller.models import Cohort, IncidentRecord, ProposalPackage, assign_cohort
from controller.stats_engine import (
    StatsEngine,
    benjamini_hochberg_significant,
    two_prop_z,
)
from controller.store import AuditStore


def test_shared_cohort_matching_and_ordering():
    cohorts = [
        Cohort(id="senior_ta", where={"age_band": ["60+"], "lang": ["ta"]}),
        Cohort(id="senior_60plus", where={"age_band": ["60+"]}),
        Cohort(id="young_tier1", where={"age_band": ["18-30"], "city_tier": ["1"]}),
    ]
    assert assign_cohort(cohorts, {"age_band": "60+", "lang": "ta", "city_tier": "2"}) == "senior_ta"
    assert assign_cohort(cohorts, {"age_band": "60+", "lang": "hi", "city_tier": "1"}) == "senior_60plus"
    assert assign_cohort(cohorts, {"age_band": "18-30", "lang": "en", "city_tier": "1"}) == "young_tier1"
    assert assign_cohort(cohorts, {"age_band": "31-45", "lang": "en", "city_tier": "2"}) == "default"


def test_benjamini_hochberg_fdr_control():
    p_vals = {
        "W2": 0.004,  # significant
        "W3": 0.022,  # significant at alpha=0.05 (k=2, m=3 -> 2/3*0.05 = 0.033)
        "W4": 0.210,  # noise
    }
    winners = benjamini_hochberg_significant(p_vals, alpha=0.05)
    assert winners == {"W2", "W3"}


def test_tvd_and_auto_apply_step_bounds(tmp_path: Path):
    store = AuditStore(tmp_path / "test.db")
    gov = GovernanceManager(store)

    p = {"D1": 1.0}
    q = {"D1": 0.5, "D2": 0.5}
    assert abs(total_variation_distance(p, q) - 0.5) < 1e-6

    wf_meta = {
        "workflow_id": "ks_cancel",
        "tenant_id": "kestrel_finance",
        "governance": {
            "auto_apply": {
                "enabled": True,
                "allowed_dims": ["schedule", "caller_id", "welcome_message", "voice"],
                "max_shift": 0.2,
            }
        },
    }
    cur_cfg = {
        "cohorts": [],
        "allocations": {"default": {"caller_id": {"D1": 1.0}, "welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}}},
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }
    target_cfg = {
        "cohorts": [{"id": "senior_60plus", "where": {"age_band": ["60+"]}}],
        "allocations": {"default": {"caller_id": {"D1": 0.4, "D2": 0.6}, "welcome_message": {"W2": 1.0}, "voice": {"V1": 1.0}}},
        "prompt_version": "P1",
        "schedule": {"hour_weights": {"default": {"10": 2.0}, "senior_60plus": {"10": 3.0}}, "lead_preferred_hour": {}},
    }

    step = gov.build_auto_apply_step(wf_meta, cur_cfg, target_cfg, allow_variant_dims=False)
    assert step is not None
    # Disallowed dim 'cohorts' must NOT be modified by auto_apply
    assert step["cohorts"] == []
    # Only active cohort ('default') hour_weights should be included
    assert "default" in step["schedule"]["hour_weights"]
    assert "senior_60plus" not in step["schedule"]["hour_weights"]
    # Caller-ID shift must be bounded strictly below max_shift=0.2
    new_did = step["allocations"]["default"]["caller_id"]
    assert total_variation_distance({"D1": 1.0}, new_did) <= 0.2
    assert set(compute_changed_dims(cur_cfg, step)).issubset({"schedule", "caller_id"})
    store.close()


def test_did_degradation_and_variant_decay_detection():
    cfg = ControllerConfig()
    ing = EventIngestor(cfg)
    stats = StatsEngine(cfg)
    acc = ing.get_workflow("ks_cancel")

    # Simulate healthy D1 (28% pickup) and degraded D2 (8% pickup)
    acc.did_recent_window["D1"] = deque([1 if i % 4 == 0 else 0 for i in range(80)], maxlen=90)
    acc.did_recent_window["D2"] = deque([1 if i % 12 == 0 else 0 for i in range(80)], maxlen=90)
    degraded = stats.detect_degraded_dids(acc, ["D1", "D2", "D3"])
    assert "D2" in degraded
    assert "D1" not in degraded

    # Simulate variant W3 peaking at 0.70 and decaying to 0.46 while W2 stays at 0.62
    wf_meta = {
        "workflow_id": "nw_renewal",
        "objective": {"primary": "engagement_rate"},
        "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2"], "caller_id": ["D1", "D2"]},
    }
    acc_nw = ing.get_workflow("nw_renewal")
    acc_nw.live_global_variants[("welcome_message", "W3")] = [250, 145]
    acc_nw.live_global_variants[("welcome_message", "W2")] = [150, 93]
    acc_nw.variant_peak_rate[("welcome_message", "W3")] = 0.70
    acc_nw.variant_peak_samples[("welcome_message", "W3")] = 90
    acc_nw.variant_recent_window[("welcome_message", "W3")] = deque([1 if i % 2 == 0 else 0 for i in range(90)], maxlen=90)
    acc_nw.variant_recent_window[("welcome_message", "W2")] = deque([1 if i % 3 != 0 else 0 for i in range(90)], maxlen=90)

    decayed = stats.detect_decaying_variants(acc_nw, wf_meta)
    assert ("welcome_message", "W3") in decayed


def test_audit_store_and_cli_commands(tmp_path: Path):
    db_file = tmp_path / "audit.db"
    store = AuditStore(db_file)
    pkg = ProposalPackage(
        proposal_id="prop_ks_1",
        workflow_id="ks_cancel",
        tenant_id="kestrel_finance",
        base_version=1,
        proposed_config={"prompt_version": "P3"},
        changed_dims=["prompt_version"],
        change_summary="Fix hallucination spike P2 -> P3",
        diff={"prompt_version": {"from": "P2", "to": "P3"}},
        affected_leads=900,
        dry_run={
            "baseline_pickup_rate": 0.24,
            "projected_pickup_rate": 0.25,
            "expected_pickup_delta": 0.01,
            "baseline_engagement_rate": 0.51,
            "projected_engagement_rate": 0.53,
            "expected_engagement_delta": 0.02,
        },
        impact_score=0.92,
        rationale="Switch from P2 to validated grounded P3",
        evidence={"validation_run_id": "val_123", "pass_rate": 0.99, "n": 300},
        is_urgent_incident=True,
    )
    store.record_proposal(pkg, "ap_1", "applied", 5025, "D3T11:45:00Z")
    store.record_decision_audit(
        decision_id="apply_prop_ks_1",
        sim_minute=5055,
        ts="D3T12:15:00Z",
        workflow_id="ks_cancel",
        tenant_id="kestrel_finance",
        decision_type="approval_applied",
        actor="controller_approved",
        base_version=1,
        new_version=2,
        proposal_id="prop_ks_1",
        approval_id="ap_1",
        changed_dims=["prompt_version"],
        change_summary=pkg.change_summary,
        rationale=pkg.rationale,
        diff=pkg.diff,
        dry_run=pkg.dry_run,
        affected_leads=900,
        impact_score=0.92,
        evidence=pkg.evidence,
        pre_kpis={"dials": 100, "pickup_rate": 0.24, "engagement_rate": 0.51, "hallucination_rate": 0.06},
    )
    store.update_decision_post_kpis(
        "apply_prop_ks_1",
        {"sample_dials": 120, "pickup_rate": 0.26, "engagement_rate": 0.54, "hallucination_rate": 0.008},
        "verified",
    )
    store.record_incident(
        IncidentRecord(
            incident_id="inc_1",
            workflow_id="ks_cancel",
            tenant_id="kestrel_finance",
            incident_type="hallucination_spike",
            detected_at_minute=5025,
            detected_at_ts="D3T11:45:00Z",
            status="resolved",
            details={"bad_prompt_version": "P2"},
            mitigation_proposal_id="prop_ks_1",
            resolved_at_minute=5160,
            resolved_at_ts="D3T14:00:00Z",
            verification_summary="Verified hallucination_rate=0.008 on P3",
        )
    )
    store.record_loop_metric(5160, "D3T14:00:00Z", 12.5, 4500, 180000.0, 0, 1, 94.0, 34.0, [])

    exp_out = cmd_explain(store, "ks_cancel")
    assert "Who Approved / Actor : controller_approved (approval_id=ap_1, proposal_id=prop_ks_1)" in exp_out
    assert "What Happened After  : pickup=0.26, engagement=0.54, hallucination=0.008" in exp_out

    sup_out = cmd_supervisor_view(store)
    assert "SUPERVISOR CONSOLIDATED OPERATIONS VIEW" in sup_out

    hlt_out = cmd_health(store)
    assert "Total Events Ingested : 4,500" in hlt_out
    store.close()
