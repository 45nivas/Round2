"""Statistical engine for variant experimentation, Empirical Bayes shrinkage, DID health, and decay rollback (C5, C6, C7)."""
import math
from typing import Any

from controller.cohort_engine import CohortEngine
from controller.config import ControllerConfig, DEFAULT_CONFIG
from controller.ingestor import EventIngestor, WorkflowAccumulator
from controller.models import Cohort


def normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_prop_z(s1: float, n1: float, s2: float, n2: float) -> float:
    """Two-proportion z-statistic testing p1 - p2 != 0."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p1 = s1 / n1
    p2 = s2 / n2
    p_pool = (s1 + s2) / (n1 + n2)
    if p_pool <= 0.0 or p_pool >= 1.0:
        return 0.0
    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se <= 1e-9:
        return 0.0
    return (p1 - p2) / se


def benjamini_hochberg_significant(p_values: dict[str, float], alpha: float = 0.05) -> set[str]:
    """Return subset of keys whose one-sided p-values pass Benjamini-Hochberg FDR control at level `alpha`."""
    if not p_values:
        return set()
    m = len(p_values)
    sorted_items = sorted(p_values.items(), key=lambda kv: kv[1])
    max_k = -1
    for k_idx, (_key, p_val) in enumerate(sorted_items, start=1):
        threshold = (k_idx / m) * alpha
        if p_val <= threshold:
            max_k = k_idx
    if max_k == -1:
        return set()
    return {key for key, _ in sorted_items[:max_k]}


class StatsEngine:
    """Deterministic statistical engine for C5 (DID health), C6 (experiments), and C7 (decay & rollback)."""

    def __init__(self, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self.cohort_engine = CohortEngine(cfg)

    # -------------------------------------------------------------------------
    # C5: Caller-ID Health Detection, Safe Exploration, and Rotation
    # -------------------------------------------------------------------------
    def detect_degraded_dids(self, acc: WorkflowAccumulator, options_dids: list[str]) -> set[str]:
        """Detect caller IDs whose recent pickup rate has degraded significantly below peers or history."""
        recent_rates: dict[str, tuple[float, int, int]] = {}
        for did in options_dids:
            win = acc.did_recent_window.get(did)
            if win and len(win) >= self.cfg.did_min_dials_for_health:
                s = sum(win)
                n = len(win)
                recent_rates[did] = (s / n, s, n)
            else:
                live = acc.live_did_pickup.get(did, [0, 0])
                if live[0] >= self.cfg.did_min_dials_for_health:
                    recent_rates[did] = (live[1] / live[0], live[1], live[0])

        if not recent_rates:
            return set(acc.degraded_dids)

        best_did = max(recent_rates.keys(), key=lambda d: recent_rates[d][0])
        best_rate, best_s, best_n = recent_rates[best_did]

        newly_degraded: set[str] = set()
        for did, (r, s, n) in recent_rates.items():
            # Compare against best active peer DID in recent window
            if did != best_did:
                drop_vs_best = best_rate - r
                z_vs_best = two_prop_z(s, n, best_s, best_n)
                if drop_vs_best >= self.cfg.did_degradation_drop and z_vs_best <= self.cfg.did_degradation_z:
                    newly_degraded.add(did)
                    continue

            # Compare against DID's own historical pickup baseline
            hist = acc.history_did_pickup.get(did, [0, 0])
            if hist[0] >= 200:
                hist_rate = hist[1] / hist[0]
                drop_vs_hist = hist_rate - r
                z_vs_hist = two_prop_z(s, n, hist[1], hist[0])
                if drop_vs_hist >= self.cfg.did_degradation_drop + 0.01 and z_vs_hist <= self.cfg.did_degradation_z:
                    newly_degraded.add(did)

        # If a previously degraded DID was probed and recovered close to best_rate, unflag it; otherwise keep flagged
        for prev_d in list(acc.degraded_dids):
            if prev_d in recent_rates:
                r, s, n = recent_rates[prev_d]
                if best_rate - r < 0.03:
                    acc.degraded_dids.discard(prev_d)

        acc.degraded_dids |= newly_degraded
        return set(acc.degraded_dids)

    def compute_did_allocation(
        self,
        acc: WorkflowAccumulator,
        wf_meta: dict[str, Any],
        current_did_alloc: dict[str, float],
    ) -> dict[str, float]:
        """Compute safe caller-ID allocation weights across workflow options (C5)."""
        options_dids = [str(d) for d in wf_meta["options"]["caller_id"]]
        if len(options_dids) <= 1:
            return {options_dids[0]: 1.0} if options_dids else {}

        degraded = self.detect_degraded_dids(acc, options_dids)
        primary_kpi = wf_meta["objective"]["primary"]

        # Classify DIDs into tested (>= 45 live dials or >= 200 history dials) vs untested
        tested_healthy: dict[str, tuple[float, int, int]] = {}
        untested: list[str] = []

        for did in options_dids:
            if did in degraded:
                continue
            win = acc.did_recent_window.get(did)
            live = acc.live_did_pickup.get(did, [0, 0])
            hist = acc.history_did_pickup.get(did, [0, 0])

            if win and len(win) >= 45:
                s = sum(win)
                n = len(win)
                tested_healthy[did] = (s / n, s, n)
            elif live[0] >= 45:
                tested_healthy[did] = (live[1] / live[0], live[1], live[0])
            elif hist[0] >= 200 and live[0] == 0:
                tested_healthy[did] = (hist[1] / hist[0], hist[1], hist[0])
            else:
                untested.append(did)

        # Fallback if all DIDs appeared degraded
        if not tested_healthy and not untested:
            best_fallback = max(
                options_dids,
                key=lambda d: (acc.live_did_pickup.get(d, [0, 0])[1] + 1) / (acc.live_did_pickup.get(d, [0, 0])[0] + 4),
            )
            return {best_fallback: 1.0}

        # Guardrail-aware canary budget for untested DIDs
        if untested:
            canary_total = (
                self.cfg.primary_dim_canary_budget
                if primary_kpi == "pickup_rate"
                else self.cfg.guardrail_dim_canary_budget
            )
            # Cap per-untested-DID probe at 0.04 when pickup is guardrail so a bad untested DID never hurts didΔ
            max_per_untested = 0.12 if primary_kpi == "pickup_rate" else 0.04
            canary_total = min(canary_total, max_per_untested * len(untested))
        else:
            canary_total = 0.0

        remaining_mass = max(0.0, 1.0 - canary_total)
        alloc: dict[str, float] = {}

        if tested_healthy:
            # Rank healthy tested DIDs by empirical pickup rate
            sorted_dids = sorted(tested_healthy.keys(), key=lambda d: tested_healthy[d][0], reverse=True)
            best_d = sorted_dids[0]
            best_r, best_s, best_n = tested_healthy[best_d]

            # Keep all healthy DIDs that are not significantly worse than best_d
            competitive: list[str] = [best_d]
            for d in sorted_dids[1:]:
                r, s, n = tested_healthy[d]
                z = two_prop_z(best_s, best_n, s, n)
                if best_r - r < 0.030 and z < 1.85:
                    competitive.append(d)

            if len(competitive) == 1:
                # Single clear winner among healthy DIDs; keep small 0.10 share on 2nd-best healthy DID if available
                if len(sorted_dids) >= 2 and (best_r - tested_healthy[sorted_dids[1]][0] < 0.05):
                    alloc[best_d] = round(remaining_mass * 0.88, 4)
                    alloc[sorted_dids[1]] = round(remaining_mass * 0.12, 4)
                else:
                    alloc[best_d] = round(remaining_mass, 4)
            else:
                # Weight competitive healthy DIDs tilted toward higher observed pickup
                scores = {d: max(0.01, tested_healthy[d][0] ** 2) for d in competitive}
                tot_sc = sum(scores.values())
                for d in competitive:
                    alloc[d] = round(remaining_mass * (scores[d] / tot_sc), 4)

        if untested:
            per_u = round(canary_total / len(untested), 4)
            for u in untested:
                alloc[u] = per_u

        # Normalize and drop zero weights
        alloc = {k: v for k, v in alloc.items() if v > 0.001}
        tot = sum(alloc.values())
        if tot > 0:
            alloc = {k: round(v / tot, 4) for k, v in alloc.items()}
        return alloc

    # -------------------------------------------------------------------------
    # C7: Variant Decay Detection
    # -------------------------------------------------------------------------
    def detect_decaying_variants(self, acc: WorkflowAccumulator, wf_meta: dict[str, Any]) -> set[tuple[str, str]]:
        """Detect welcome_message or voice variants whose recent engagement rate has decayed (C7)."""
        for dim in ("welcome_message", "voice"):
            opts = [str(o) for o in wf_meta["options"][dim]]
            recent_stats: dict[str, tuple[float, int, int]] = {}
            for opt in opts:
                win = acc.variant_recent_window.get((dim, opt))
                if win and len(win) >= self.cfg.decay_min_recent_calls:
                    s = sum(win)
                    n = len(win)
                    recent_stats[opt] = (s / n, s, n)

            if not recent_stats:
                continue

            best_opt = max(recent_stats.keys(), key=lambda o: recent_stats[o][0])
            best_r, best_s, best_n = recent_stats[best_opt]

            for opt, (cur_r, cur_s, cur_n) in recent_stats.items():
                key = (dim, opt)
                peak_r = acc.variant_peak_rate.get(key)
                peak_n = acc.variant_peak_samples.get(key, 0)

                # Condition 1: Significant drop from this variant's own earlier rolling peak
                if peak_r is not None and peak_n >= 90:
                    drop_from_peak = peak_r - cur_r
                    z_peak = two_prop_z(cur_s, cur_n, peak_r * peak_n, peak_n)
                    if drop_from_peak >= self.cfg.decay_min_drop and z_peak <= self.cfg.decay_z_threshold:
                        acc.decayed_variants.add(key)
                        continue

                # Condition 2: Cumulative historical leader that collapsed in the recent window vs cumulative earlier calls
                tot_conn, tot_eng = acc.live_global_variants.get(key, [0, 0])
                if tot_conn >= cur_n + 120:
                    early_n = tot_conn - cur_n
                    early_s = tot_eng - cur_s
                    early_r = early_s / early_n
                    drop_vs_early = early_r - cur_r
                    z_early = two_prop_z(cur_s, cur_n, early_s, early_n)
                    if (
                        drop_vs_early >= self.cfg.decay_min_drop
                        and z_early <= self.cfg.decay_z_threshold
                        and (best_r - cur_r >= 0.025)
                    ):
                        acc.decayed_variants.add(key)

        return set(acc.decayed_variants)

    # -------------------------------------------------------------------------
    # C6: Empirical Bayes Variant Allocation & Multiple-Comparison Winner Selection
    # -------------------------------------------------------------------------
    def compute_cohort_allocations(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        wf_meta: dict[str, Any],
        cohorts: list[Cohort],
        current_allocations: dict[str, Any],
    ) -> dict[str, dict[str, dict[str, float]]]:
        """Compute cohort-specific and default allocations for welcome_message, voice, and caller_id (C5, C6, C7)."""
        acc = ingestor.get_workflow(workflow_id)
        self.detect_decaying_variants(acc, wf_meta)

        cur_default_did = current_allocations.get("default", {}).get("caller_id", {})
        did_alloc = self.compute_did_allocation(acc, wf_meta, cur_default_did)

        cell_to_cid = self.cohort_engine.map_cells_to_cohorts(ingestor, workflow_id, cohorts)
        cohort_ids = ["default"] + [c.id for c in cohorts]
        primary_kpi = wf_meta["objective"]["primary"]

        # Aggregate live connected & engaged per (cohort_id, dim, opt)
        cid_var_stats: dict[tuple[str, str, str], list[int]] = {}
        for (f_idx, dim, opt), (conn, eng) in acc.live_cell_variants.items():
            cid = cell_to_cid.get(f_idx, "default")
            k = (cid, dim, opt)
            if k not in cid_var_stats:
                cid_var_stats[k] = [conn, eng]
            else:
                cid_var_stats[k][0] += conn
                cid_var_stats[k][1] += eng

        # Also aggregate history baseline per (cohort_id, dim, opt) for the incumbent arm (W1, V1)
        cid_hist_stats: dict[tuple[str, str, str], list[int]] = {}
        for (f_idx, dim, opt), (conn, eng) in acc.history_cell_variants.items():
            cid = cell_to_cid.get(f_idx, "default")
            k = (cid, dim, opt)
            if k not in cid_hist_stats:
                cid_hist_stats[k] = [conn, eng]
            else:
                cid_hist_stats[k][0] += conn
                cid_hist_stats[k][1] += eng

        # Compute allocations per cohort
        new_allocations: dict[str, dict[str, dict[str, float]]] = {
            "default": {"caller_id": did_alloc}
        }
        for cid in cohort_ids:
            if cid not in new_allocations:
                new_allocations[cid] = {}
            for dim in ("welcome_message", "voice"):
                dim_alloc = self._allocate_single_dimension(
                    acc=acc,
                    wf_meta=wf_meta,
                    cid=cid,
                    dim=dim,
                    primary_kpi=primary_kpi,
                    cid_var_stats=cid_var_stats,
                    cid_hist_stats=cid_hist_stats,
                )
                new_allocations[cid][dim] = dim_alloc

        # Prune redundant cohort dimension tables that are identical to 'default'
        default_table = new_allocations["default"]
        for c in cohorts:
            cid = c.id
            c_table = new_allocations.get(cid, {})
            if (
                c_table.get("welcome_message") == default_table.get("welcome_message")
                and c_table.get("voice") == default_table.get("voice")
            ):
                new_allocations.pop(cid, None)

        return new_allocations

    def _allocate_single_dimension(
        self,
        acc: WorkflowAccumulator,
        wf_meta: dict[str, Any],
        cid: str,
        dim: str,
        primary_kpi: str,
        cid_var_stats: dict[tuple[str, str, str], list[int]],
        cid_hist_stats: dict[tuple[str, str, str], list[int]],
    ) -> dict[str, float]:
        opts = [str(o) for o in wf_meta["options"][dim]]
        if len(opts) <= 1:
            return {opts[0]: 1.0} if opts else {}

        incumbent = opts[0]  # W1 or V1

        # 1. Check global sample sizes to determine if we are still in initial Canary Exploration
        global_counts: dict[str, tuple[int, int]] = {}
        for opt in opts:
            if (dim, opt) in acc.decayed_variants:
                win = acc.variant_recent_window.get((dim, opt))
                if win and len(win) > 0:
                    global_counts[opt] = (len(win), sum(win))
                else:
                    global_counts[opt] = (0, 0)
            else:
                gv = acc.live_global_variants.get((dim, opt), [0, 0])
                # Softly blend history for incumbent if live count is still small
                if opt == incumbent and gv[0] < 80:
                    hv = acc.history_global_variants.get((dim, opt), [0, 0])
                    if hv[0] > 0:
                        hr = hv[1] / hv[0]
                        pseudo_n = min(120, hv[0])
                        global_counts[opt] = (gv[0] + pseudo_n, int(round(gv[1] + pseudo_n * hr)))
                    else:
                        global_counts[opt] = (gv[0], gv[1])
                else:
                    global_counts[opt] = (gv[0], gv[1])

        untested = [
            opt
            for opt in opts
            if opt != incumbent
            and (dim, opt) not in acc.decayed_variants
            and acc.live_global_variants.get((dim, opt), [0, 0])[0] < 35
        ]

        # If any challenger arm is still untested (< 35 global connected calls), run Guardrail-Safe Canary Probe
        if untested and all(acc.live_global_variants.get((dim, o), [0, 0])[0] < 60 for o in opts if o != incumbent):
            canary_budget = (
                self.cfg.primary_dim_canary_budget
                if primary_kpi == "engagement_rate"
                else self.cfg.guardrail_dim_canary_budget
            )
            challengers = [o for o in opts if o != incumbent and (dim, o) not in acc.decayed_variants]
            if not challengers:
                return {incumbent: 1.0}
            per_chal = round(canary_budget / len(challengers), 4)
            inc_share = round(1.0 - per_chal * len(challengers), 4)
            res = {incumbent: inc_share}
            for ch in challengers:
                res[ch] = per_chal
            return res

        # 2. Compute Empirical Bayes posterior mean and effective sample size for each option in cohort `cid`
        eb_rates: dict[str, float] = {}
        eff_stats: dict[str, tuple[float, float]] = {}  # opt -> (eff_successes, eff_trials)
        prior_strength = self.cfg.eb_global_prior_strength

        for opt in opts:
            if (dim, opt) in acc.decayed_variants:
                continue
            g_n, g_s = global_counts.get(opt, (0, 0))
            if g_n <= 0:
                continue
            g_rate = g_s / g_n

            c_n, c_s = cid_var_stats.get((cid, dim, opt), [0, 0])
            if opt == incumbent and c_n < 40:
                hc_n, hc_s = cid_hist_stats.get((cid, dim, opt), [0, 0])
                if hc_n > 0:
                    h_r = hc_s / hc_n
                    p_n = min(60, hc_n)
                    c_n += p_n
                    c_s += int(round(p_n * h_r))

            # Empirical Bayes shrinkage of cohort rate toward global workflow rate
            eb_r = (c_s + prior_strength * g_rate) / (c_n + prior_strength)
            eb_rates[opt] = eb_r
            eff_n = c_n + min(float(g_n), prior_strength)
            eff_s = eb_r * eff_n
            eff_stats[opt] = (eff_s, eff_n)

        if not eb_rates:
            return {incumbent: 1.0}

        # 3. Multiple comparisons test (Benjamini-Hochberg) against incumbent and runner-up
        inc_r = eb_rates.get(incumbent, min(eb_rates.values()))
        inc_s, inc_n = eff_stats.get(incumbent, (inc_r * 50.0, 50.0))

        p_values_vs_inc: dict[str, float] = {}
        z_scores_vs_inc: dict[str, float] = {}
        for opt, r in eb_rates.items():
            if opt == incumbent:
                continue
            s, n = eff_stats[opt]
            z = two_prop_z(s, n, inc_s, inc_n)
            z_scores_vs_inc[opt] = z
            p_values_vs_inc[opt] = 1.0 - normal_cdf(z)

        bh_winners = benjamini_hochberg_significant(p_values_vs_inc, alpha=self.cfg.fdr_alpha)

        sorted_opts = sorted(eb_rates.keys(), key=lambda o: eb_rates[o], reverse=True)
        best_opt = sorted_opts[0]
        best_r = eb_rates[best_opt]
        best_s, best_n = eff_stats[best_opt]

        # If the incumbent itself is the best option
        if best_opt == incumbent:
            # Keep a small canary only on challengers that are not clearly worse than incumbent
            viable_chals = [
                o
                for o in sorted_opts[1:]
                if (inc_r - eb_rates[o] < 0.012)
                and acc.live_global_variants.get((dim, o), [0, 0])[0] < 180
            ]
            if not viable_chals:
                return {incumbent: 1.0}
            chal_share = 0.08 if primary_kpi == "pickup_rate" else 0.12
            per_c = round(chal_share / len(viable_chals), 4)
            res = {incumbent: round(1.0 - per_c * len(viable_chals), 4)}
            for c_opt in viable_chals:
                res[c_opt] = per_c
            return res

        # A challenger `best_opt` has the highest Empirical Bayes rate
        raw_global_n = acc.live_global_variants.get((dim, best_opt), [0, 0])[0]
        z_best_vs_inc = z_scores_vs_inc.get(best_opt, 0.0)
        lift_vs_inc = best_r - inc_r

        second_opt = sorted_opts[1] if len(sorted_opts) >= 2 else incumbent
        second_r = eb_rates[second_opt]
        second_s, second_n = eff_stats[second_opt]
        z_best_vs_second = two_prop_z(best_s, best_n, second_s, second_n)

        # Case A: Statistically significant winner (BH FDR significant OR strong z-score >= 1.96 with n >= 60)
        if (
            (best_opt in bh_winners or z_best_vs_inc >= self.cfg.promote_z_threshold)
            and raw_global_n >= self.cfg.min_arm_connected_for_promo
            and lift_vs_inc >= 0.015
        ):
            # If best_opt also clearly dominates second_opt, allocate 88-92% to best_opt and 8-12% to second_opt
            if z_best_vs_second >= 1.40 or (best_r - second_r >= 0.025):
                canary = self.cfg.active_runner_up_canary
                return {best_opt: round(1.0 - canary, 4), second_opt: round(canary, 4)}
            # Two challengers both beat incumbent and are close to each other
            return {best_opt: 0.60, second_opt: 0.40}

        # Case B: Promising challenger (z >= 1.28 or lift >= 0.018 with n >= 40) -> graduate share safely
        if (z_best_vs_inc >= self.cfg.graduate_z_threshold or lift_vs_inc >= 0.018) and raw_global_n >= 40:
            if second_opt != incumbent and eb_rates[second_opt] > inc_r + 0.012:
                return {best_opt: 0.45, second_opt: 0.30, incumbent: 0.25}
            return {best_opt: 0.55, incumbent: 0.45}

        # Case C: Slight positive edge but not yet significant -> cautious exploration on top 2, prune losers
        if lift_vs_inc > 0.005:
            share = 0.15 if primary_kpi == "pickup_rate" else 0.25
            return {incumbent: round(1.0 - share, 4), best_opt: round(share, 4)}

        return {incumbent: 1.0}
