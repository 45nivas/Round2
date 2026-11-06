"""Configuration and statistical hyperparameters for the Outcome-First Controller."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ControllerConfig:
    # Cohort discovery & sizing (C2, C3)
    max_cohorts: int = 8
    min_cohort_history_connected: int = 200
    min_cohort_share: float = 0.06  # Each cohort must represent >= 6% of workflow traffic

    # Call-time schedule optimization (C4)
    hour_weight_smoothing_prior: float = 40.0  # Beta-Binomial pseudocounts for hour pickup smoothing
    hour_weight_min: float = 0.25              # Never 0 inside dialling window so capacity is never wasted
    hour_weight_max: float = 4.00              # Cap max hour weight multiplier
    hour_weight_exponent: float = 2.5          # Sharpness of hour weight separation (p_hr / p_mean) ** exp

    # Guardrail-aware safe exploration budgets (C5, C6)
    # When a dimension affects the guardrail KPI, keep exploration small so initial probe never harms guardrail
    guardrail_dim_canary_budget: float = 0.08  # Total share across all untested arms on guardrail dimension
    primary_dim_canary_budget: float = 0.22    # Total share across all untested arms on primary KPI dimension
    active_runner_up_canary: float = 0.12      # Share kept on runner-up arm after winner promotion (for decay comparison)

    # Empirical Bayes & Winner Selection (C6)
    eb_global_prior_strength: float = 60.0     # Shrinkage pseudocounts toward global workflow variant mean
    min_arm_connected_for_promo: int = 60      # Minimum connected calls on an arm before promoting
    min_cohort_arm_connected: int = 35         # Minimum connected calls in cohort before cohort-specific override
    fdr_alpha: float = 0.05                    # Benjamini-Hochberg false discovery rate threshold
    graduate_z_threshold: float = 1.28         # z-score (~90% one-sided) to graduate a promising arm to 0.35 share
    promote_z_threshold: float = 1.96          # z-score (~97.5% one-sided) to promote a winning arm to 0.88 share

    # Variant Decay Detection & Rollback (C7)
    decay_window_calls: int = 180              # Sliding window of recent connected calls per (workflow, dim, opt)
    decay_min_recent_calls: int = 75           # Minimum calls in recent window to test for decay
    decay_min_drop: float = 0.055              # Minimum drop (5.5 pp) from peak or below best peer arm
    decay_z_threshold: float = -1.85           # One-sided z-score for significant decay

    # Caller-ID Health & Rotation (C5)
    did_window_dials: int = 200                # Sliding window of recent dials per (workflow, caller_id)
    did_min_dials_for_health: int = 60         # Minimum dials in window before flagging degradation
    did_degradation_drop: float = 0.055        # Drop below peer DIDs / baseline to flag degraded DID
    did_degradation_z: float = -2.10           # Two-proportion z-score threshold for DID degradation

    # Hallucination Incident Detection & Response (C8)
    halluc_min_connected_calls: int = 35       # Minimum connected calls on current prompt_version to trigger
    halluc_rate_multiplier: float = 2.2        # Must exceed 2.2x historical baseline hallucination rate
    halluc_min_abs_rate: float = 0.038         # Minimum absolute hallucination rate (3.8%) to trigger incident
    halluc_z_threshold: float = 2.33           # One-sided z-score (p < 0.01) vs historical baseline
    validation_sample_size: int = 300          # Synthetic conversations per validation run (3 credits)
    post_fix_verify_calls: int = 50            # Connected calls after prompt fix to verify resolution

    # Governance & Fatigue Management (C9)
    supervisor_edit_grace_minutes: int = 360   # Respect benign supervisor edits for at least 6 hours
    min_repropose_interval_minutes: int = 360  # Minimum time between non-urgent proposals on same workflow
    reserve_fatigue_slots: int = 1             # Keep 1 request/24h slot reserved for urgent incidents
    min_impact_score_to_propose: float = 0.08  # Avoid noisy micro-proposals


DEFAULT_CONFIG = ControllerConfig()
