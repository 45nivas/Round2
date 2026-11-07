"""Statistical engine for variant experimentation, covariate-adjusted Empirical Bayes, DID health, and decay rollback (C5, C6, C7)."""
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
    # C5: Caller-ID Health Detection and Load-Balanced Rotation
    # -------------------------------------------------------------------------
    def detect_degraded_dids(self, acc: WorkflowAccumulator, options_dids: list[str]) -> set[str]:
        """Detect caller IDs whose pickup rate is degraded relative to concurrent healthy peer DIDs (C5).
        Comparing against concurrent peer DIDs inherently controls for diurnal variation and campaign mix.
        """
        recent_stats: dict[str, tuple[int, int, float]] = {}
        for did in options_dids:
            win = acc.did_recent_window.get(did)
            if win and len(win) >= 35:
                recent_slice = list(win)[-120:]
                s = sum(recent_slice)
                n = len(recent_slice)
                recent_stats[did] = (s, n, s / n)

        if not recent_stats:
            return set(acc.degraded_dids)

        newly_degraded: set[str] = set()
        for did, (s, n, r) in recent_stats.items():
            if did in acc.degraded_dids:
                continue

            # Concurrent peer DIDs that are not already degraded
            peers = [
                o for o in options_dids
                if o != did and o not in acc.degraded_dids and o in recent_stats
            ]
            if not peers:
                continue

            peer_s = sum(recent_stats[o][0] for o in peers)
            peer_n = sum(recent_stats[o][1] for o in peers)
            if peer_n < 40:
                continue

            peer_r = peer_s / peer_n
            if peer_r < 0.14:
                continue

            drop_vs_peer = peer_r - r
            z_vs_peer = two_prop_z(s, n, peer_s, peer_n)

            # Degraded if pickup rate is <= 58% of concurrent healthy peers with high statistical confidence
            if r <= 0.58 * peer_r and drop_vs_peer >= 0.075 and z_vs_peer <= -2.40:
                newly_degraded.add(did)

        acc.degraded_dids |= newly_degraded
        return set(acc.degraded_dids)

    def compute_did_allocation(
        self,
        acc: WorkflowAccumulator,
        wf_meta: dict[str, Any],
        current_did_alloc: dict[str, float],
    ) -> dict[str, float]:
        """Compute load-balanced caller-ID allocation across healthy DIDs (C5).
        Untested DIDs (< 100 history dials) get a cautious 0.04 canary probe
        until they accumulate >= 50 live dials to verify healthy pickup,
        preventing pre-degraded untested DIDs (e.g. D4) from hurting did_health.
        Verified healthy DIDs share traffic equally to minimize quadratic load fatigue.
        """
        options_dids = [str(d) for d in wf_meta["options"]["caller_id"]]
        if len(options_dids) <= 1:
            return {options_dids[0]: 1.0} if options_dids else {}

        degraded = self.detect_degraded_dids(acc, options_dids)

        tested: list[str] = []
        untested: list[str] = []
        for d in options_dids:
            if d in degraded:
                continue
            hist_n = acc.history_did_pickup.get(d, [0, 0])[0]
            live_n = acc.live_did_pickup.get(d, [0, 0])[0]
            if hist_n >= 100 or live_n >= 50:
                tested.append(d)
            else:
                untested.append(d)

        non_degraded = [d for d in options_dids if d not in acc.degraded_dids]
        if not non_degraded:
            non_degraded = list(options_dids)

        tested = [d for d in tested if d in non_degraded]
        untested = [d for d in untested if d in non_degraded]
        if not tested:
            tested = non_degraded
            untested = []

        if tested and untested:
            probe_w = 0.04
            rem_w = max(0.0, 1.0 - probe_w * len(untested))
            per_t = round(rem_w / len(tested), 4)
            alloc = {d: per_t for d in tested}
            for u in untested:
                alloc[u] = probe_w
            tot = sum(alloc.values())
            return {d: round(v / tot, 4) for d, v in alloc.items()}

        w = round(1.0 / len(tested), 4)
        alloc = {d: w for d in tested}
        tot = sum(alloc.values())
        if tot > 0:
            alloc = {d: round(v / tot, 4) for d, v in alloc.items()}
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

            for opt, (cur_r, cur_s, cur_n) in recent_stats.items():
                key = (dim, opt)
                tot_conn, tot_eng = acc.live_global_variants.get(key, [0, 0])
                if tot_conn < 110:
                    continue

                alt_rates: list[float] = []
                for other_opt in opts:
                    if other_opt == opt or (dim, other_opt) in acc.decayed_variants:
                        continue
                    if other_opt in recent_stats:
                        alt_rates.append(recent_stats[other_opt][0])
                    else:
                        g_c, g_e = acc.live_global_variants.get((dim, other_opt), [0, 0])
                        if g_c >= 25:
                            alt_rates.append(g_e / g_c)
                        elif other_opt == opts[0] and acc.history_totals[1] >= 100:
                            alt_rates.append(acc.history_totals[2] / acc.history_totals[1])
                alt_best_r = max(alt_rates) if alt_rates else 0.54

                if cur_r > alt_best_r + 0.015:
                    continue

                peak_r = acc.variant_peak_rate.get(key)
                peak_n = acc.variant_peak_samples.get(key, 0)

                if peak_r is not None and peak_n >= 45:
                    drop_from_peak = peak_r - cur_r
                    z_peak = two_prop_z(cur_s, cur_n, peak_r * peak_n, peak_n)
                    if drop_from_peak >= self.cfg.decay_min_drop and z_peak <= self.cfg.decay_z_threshold:
                        acc.decayed_variants.add(key)
                        continue

                if tot_conn >= cur_n + 50:
                    early_n = tot_conn - cur_n
                    early_s = tot_eng - cur_s
                    early_r = early_s / early_n
                    drop_vs_early = early_r - cur_r
                    z_early = two_prop_z(cur_s, cur_n, early_s, early_n)
                    if drop_vs_early >= self.cfg.decay_min_drop and z_early <= self.cfg.decay_z_threshold:
                        acc.decayed_variants.add(key)

        return set(acc.decayed_variants)

    # -------------------------------------------------------------------------
    # C6: Covariate-Adjusted Empirical Bayes Variant Allocation
    # -------------------------------------------------------------------------
    def compute_cell_baseline_rates(self, acc: WorkflowAccumulator) -> tuple[dict[int, float], float]:
        """Compute baseline engagement rate `p_base(f_idx)` on incumbent (W1, V1) from 14-day history."""
        h_conn = max(1, acc.history_totals[1])
        h_eng = acc.history_totals[2]
        global_base = h_eng / h_conn if acc.history_totals[1] > 0 else 0.52

        cell_counts: dict[int, list[float]] = {}
        for (f_idx, dim, _opt), (c, e) in acc.history_cell_variants.items():
            if dim == "welcome_message":
                entry = cell_counts.setdefault(f_idx, [0.0, 0.0])
                entry[0] += c
                entry[1] += e

        p_base: dict[int, float] = {0: global_base}
        for f_idx, (c, e) in cell_counts.items():
            p_base[f_idx] = (e + 10.0 * global_base) / (c + 10.0)

        return p_base, global_base

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

        p_base, global_base = self.compute_cell_baseline_rates(acc)
        all_f_idxs = {f_idx for f_idx in range(1, len(ingestor.feat_idx_to_dict))}

        # Single-value 1-way feature slices (exact match on feature)
        single_slices: list[tuple[str, tuple[str, ...], set[int]]] = [
            ("age_band", ("60+",), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") == "60+"}),
            ("age_band", ("46-60",), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") == "46-60"}),
            ("age_band", ("18-30",), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") == "18-30"}),
            ("age_band", ("31-45",), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") == "31-45"}),
            ("city_tier", ("1",), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("city_tier") == "1"}),
        ]
        for lang in wf_meta.get("languages", []):
            l_str = str(lang)
            single_slices.append(
                ("lang", (l_str,), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("lang") == l_str})
            )

        # Adjacent age-band pairs (only used when both constituent age bands agree in direction)
        paired_age_slices: list[tuple[str, tuple[str, ...], set[int]]] = [
            ("age_band", ("46-60", "60+"), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") in ("46-60", "60+")}),
            ("age_band", ("18-30", "31-45"), {i for i in all_f_idxs if ingestor.feat_idx_to_dict[i].get("age_band") in ("18-30", "31-45")}),
        ]

        cell_to_cid = self.cohort_engine.map_cells_to_cohorts(ingestor, workflow_id, cohorts)
        cid_cells: dict[str, set[int]] = {"default": set()}
        for c in cohorts:
            cid_cells[c.id] = set()
        for f_idx in all_f_idxs:
            cid = cell_to_cid.get(f_idx, "default")
            cid_cells.setdefault(cid, set()).add(f_idx)

        cohort_ids = ["default"] + [c.id for c in cohorts]
        new_allocations: dict[str, dict[str, dict[str, float]]] = {
            "default": {"caller_id": did_alloc}
        }
        for cid in cohort_ids:
            new_allocations.setdefault(cid, {})

        for dim in ("welcome_message", "voice"):
            dim_tables = self._allocate_dimension_across_cohorts(
                acc=acc,
                wf_meta=wf_meta,
                dim=dim,
                cohorts=cohorts,
                cid_cells=cid_cells,
                single_slices=single_slices,
                paired_age_slices=paired_age_slices,
                p_base=p_base,
                global_base=global_base,
                current_allocations=current_allocations,
            )
            for cid, dist in dim_tables.items():
                new_allocations[cid][dim] = dist

        # Prune redundant cohort tables identical to 'default'
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

    def _allocate_dimension_across_cohorts(
        self,
        acc: WorkflowAccumulator,
        wf_meta: dict[str, Any],
        dim: str,
        cohorts: list[Cohort],
        cid_cells: dict[str, set[int]],
        single_slices: list[tuple[str, tuple[str, ...], set[int]]],
        paired_age_slices: list[tuple[str, tuple[str, ...], set[int]]],
        p_base: dict[int, float],
        global_base: float,
        current_allocations: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, float]]:
        opts = [str(o) for o in wf_meta["options"][dim]]
        cohort_ids = ["default"] + [c.id for c in cohorts]
        if len(opts) <= 1:
            single = {opts[0]: 1.0} if opts else {}
            return {cid: dict(single) for cid in cohort_ids}

        incumbent = opts[0]  # W1 or V1
        challengers = [o for o in opts if o != incumbent and (dim, o) not in acc.decayed_variants]
        if not challengers:
            return {cid: {incumbent: 1.0} for cid in cohort_ids}

        # For next_campaign_only workflows (nw_kyc), use a 0.11 per-challenger canary and 18-call threshold
        is_next_camp = wf_meta.get("mid_flight") == "next_campaign_only"
        min_canary_calls = 18 if is_next_camp else 24

        min_chal_calls = min(acc.live_global_variants.get((dim, o), [0, 0])[0] for o in challengers)
        if min_chal_calls < min_canary_calls:
            per_chal = round(min(0.11 if is_next_camp else 0.08, 0.21 / len(challengers)), 4)
            inc_w = round(1.0 - per_chal * len(challengers), 4)
            canary_dist = {incumbent: inc_w}
            for ch in challengers:
                canary_dist[ch] = per_chal
            return {cid: dict(canary_dist) for cid in cohort_ids}

        def _eval_slice(opt: str, f_set: set[int] | None, prior_m: float, tau: float) -> tuple[int, int, float, float, float]:
            n_tot = 0
            s_tot = 0
            mu0 = 0.0
            var0 = 0.0
            for (f_idx, d_name, o_name), (c_cnt, e_cnt) in acc.live_cell_variants.items():
                if d_name != dim or o_name != opt:
                    continue
                if f_set is not None and f_idx not in f_set:
                    continue
                pb = p_base.get(f_idx, global_base)
                n_tot += c_cnt
                s_tot += e_cnt
                mu0 += c_cnt * pb
                var0 += c_cnt * pb * (1.0 - pb)
            if n_tot == 0 or mu0 <= 1e-6:
                return 0, 0, prior_m, 0.0, 0.5
            m_eb = (s_tot + tau * prior_m) / (mu0 + tau)
            z = (s_tot - mu0) / math.sqrt(max(1e-6, var0))
            p_val = 1.0 - normal_cdf(z)
            return n_tot, s_tot, m_eb, z, p_val

        inc_n, _inc_s, inc_m_raw, _inc_z, _ = _eval_slice(incumbent, None, 1.0, 25.0)
        base_m_offset = inc_m_raw if inc_n >= 120 else 1.0

        glob_stats: dict[str, tuple[int, int, float, float, float]] = {}
        slice_stats: dict[tuple[str, str, tuple[str, ...]], tuple[int, int, float, float, float]] = {}
        all_p_values: dict[str, float] = {}

        for opt in challengers:
            gn, gs, gm, gz, gp = _eval_slice(opt, None, base_m_offset, 10.0)
            gm_rel = gm / max(0.5, base_m_offset)
            glob_stats[opt] = (gn, gs, gm_rel, gz, gp)
            all_p_values[f"glob:{opt}"] = gp

            for feat_name, feat_vals, f_set in single_slices + paired_age_slices:
                sn, ss, sm, sz, sp = _eval_slice(opt, f_set, gm, 8.0)
                sm_rel = sm / max(0.5, base_m_offset)
                slice_stats[(opt, feat_name, feat_vals)] = (sn, ss, sm_rel, sz, sp)
                if sn >= 14:
                    all_p_values[f"slice:{opt}:{feat_name}:{feat_vals}"] = sp

        bh_sig = benjamini_hochberg_significant(all_p_values, alpha=0.08)

        cid_to_cohort = {c.id: c for c in cohorts}
        out_tables: dict[str, dict[str, float]] = {}

        for cid in cohort_ids:
            c_obj = cid_to_cohort.get(cid)
            c_fset = cid_cells.get(cid, set())
            opt_scores: dict[str, tuple[float, float, bool]] = {incumbent: (1.0, 0.0, False)}

            for opt in challengers:
                gn, _gs, gm_rel, gz, _gp = glob_stats[opt]
                # Start from global estimate if globally significant or strong; else 1.0
                best_m = gm_rel if (gz >= 1.25 or gz <= -1.0) else (0.5 * gm_rel + 0.5)
                best_z = gz
                is_sig = f"glob:{opt}" in bh_sig
                best_z_abs = abs(gz) if abs(gz) >= 1.35 else 0.0

                # 1. Check exact 1-way single slices matching this cohort
                for feat_name, feat_vals, f_set in single_slices:
                    matches_cohort = False
                    if c_obj and feat_name in c_obj.where:
                        if set(c_obj.where[feat_name]) == set(feat_vals):
                            matches_cohort = True
                    elif c_fset and len(c_fset & f_set) >= 0.80 * len(c_fset):
                        matches_cohort = True
                    if not matches_cohort:
                        continue

                    sn, _ss, sm_rel, sz, _sp = slice_stats[(opt, feat_name, feat_vals)]
                    if sn < 14:
                        continue
                    if abs(sz) >= 1.30 and abs(sz) > best_z_abs:
                        best_z_abs = abs(sz)
                        best_m = sm_rel
                        best_z = sz
                        if f"slice:{opt}:{feat_name}:{feat_vals}" in bh_sig:
                            is_sig = True

                # 2. Check paired age slices ("46-60", "60+") or ("18-30", "31-45") ONLY if both constituent age bands agree
                for feat_name, feat_vals, f_set in paired_age_slices:
                    matches_cohort = False
                    if c_obj and "age_band" in c_obj.where:
                        if set(c_obj.where["age_band"]).issubset(set(feat_vals)):
                            matches_cohort = True
                    elif cid == "default" and "31-45" in feat_vals:
                        matches_cohort = True
                    if not matches_cohort:
                        continue

                    b1, b2 = feat_vals[0], feat_vals[1]
                    s1 = slice_stats.get((opt, "age_band", (b1,)))
                    s2 = slice_stats.get((opt, "age_band", (b2,)))
                    if not s1 or not s2:
                        continue
                    # Both age bands must show positive lift (> 1.015) or both negative (< 0.985)
                    both_pos = s1[2] > 1.015 and s2[2] > 1.015
                    both_neg = s1[2] < 0.985 and s2[2] < 0.985
                    if not (both_pos or both_neg):
                        continue

                    sn, _ss, sm_rel, sz, _sp = slice_stats[(opt, feat_name, feat_vals)]
                    if sn >= 18 and abs(sz) >= 1.35 and abs(sz) > best_z_abs:
                        best_z_abs = abs(sz)
                        best_m = sm_rel
                        best_z = sz
                        if f"slice:{opt}:{feat_name}:{feat_vals}" in bh_sig:
                            is_sig = True

                opt_scores[opt] = (best_m, best_z, is_sig)

            ranked = sorted(opt_scores.keys(), key=lambda o: opt_scores[o][0], reverse=True)
            best_opt = ranked[0]
            best_m, best_z, best_sig = opt_scores[best_opt]
            second_opt = ranked[1] if len(ranked) >= 2 else incumbent
            second_m, _second_z, _ = opt_scores[second_opt]

            # Sticky winner retention (hysteresis):
            # If this cohort already has an established non-incumbent winner in current_allocations,
            # do not demote it back to incumbent on minor z-score fluctuation unless it actually decays or turns negative.
            cur_cid_dist = (
                (current_allocations or {}).get(cid, {}).get(dim)
                or (current_allocations or {}).get("default", {}).get(dim, {})
            )
            existing_winner = None
            if cur_cid_dist:
                for o, w in cur_cid_dist.items():
                    if w >= 0.55 and o != incumbent and (dim, o) not in acc.decayed_variants:
                        existing_winner = o
                        break

            if existing_winner:
                ew_m, ew_z, _ = opt_scores.get(existing_winner, (1.0, 0.0, False))
                if ew_m >= 1.008 and ew_z >= 0.40:
                    canary = 0.10
                    runner_up = ranked[0] if ranked[0] != existing_winner else (ranked[1] if len(ranked) >= 2 else incumbent)
                    out_tables[cid] = {existing_winner: round(1.0 - canary, 4), runner_up: round(canary, 4)}
                    continue

            if best_opt == incumbent or best_m < 1.022 or best_z < 1.15:
                # Keep small canary only if challengers are still < 40 calls and not clearly losing
                under_tested = [
                    o
                    for o in challengers
                    if glob_stats[o][0] < 40 and opt_scores[o][0] >= 0.985 and opt_scores[o][1] > -1.0
                ]
                if under_tested:
                    per_u = 0.06
                    res = {incumbent: round(1.0 - per_u * len(under_tested), 4)}
                    for u in under_tested:
                        res[u] = per_u
                    out_tables[cid] = res
                else:
                    out_tables[cid] = {incumbent: 1.0}
                continue

            gn_best = glob_stats[best_opt][0]
            if (best_sig or best_z >= 1.60 or best_m >= 1.060) and gn_best >= min_canary_calls:
                canary = 0.10 if (best_m - second_m >= 0.035) else 0.12
                out_tables[cid] = {best_opt: round(1.0 - canary, 4), second_opt: round(canary, 4)}
            elif best_z >= 1.15 and best_m >= 1.022 and gn_best >= min_canary_calls:
                out_tables[cid] = {best_opt: 0.78, second_opt: 0.22}
            else:
                out_tables[cid] = {incumbent: 1.0}

        return out_tables
