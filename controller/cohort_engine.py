"""Cohort discovery from history and per-cohort call-time schedule optimization (C2, C3, C4)."""
from collections import defaultdict
from typing import Any

from controller.config import ControllerConfig, DEFAULT_CONFIG
from controller.ingestor import EventIngestor
from controller.models import Cohort, assign_cohort


class CohortEngine:
    """Builds the shared Cohort definitions (C2) and computes per-cohort schedule.hour_weights (C4)."""

    def __init__(self, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.cfg = cfg

    def discover_cohorts(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        wf_meta: dict[str, Any],
    ) -> list[Cohort]:
        """Discover a stable, non-fragmented ordered list of shared Cohort objects from history & live data."""
        acc = ingestor.get_workflow(workflow_id)

        # Aggregate connected calls and lead counts per feature dictionary
        cell_leads: dict[int, int] = defaultdict(int)
        for f_idx, cnt in acc.history_lead_counts.items():
            cell_leads[f_idx] += cnt
        for f_idx, cnt in acc.live_lead_counts.items():
            cell_leads[f_idx] += cnt

        cell_conn: dict[int, int] = defaultdict(int)
        for (f_idx, _hr), (_d, c) in acc.history_hour_pickup.items():
            cell_conn[f_idx] += c
        for (f_idx, _hr), (_d, c) in acc.live_hour_pickup.items():
            cell_conn[f_idx] += c

        total_leads = max(1, sum(cell_leads.values()))
        total_conn = max(1, sum(cell_conn.values()))
        min_conn = min(self.cfg.min_cohort_history_connected, max(60, int(0.05 * total_conn)))
        min_leads = int(self.cfg.min_cohort_share * total_leads)

        # Helper to measure size of a candidate Cohort among remaining unassigned feature cells
        unassigned_cells = {f_idx for f_idx in cell_leads if f_idx != 0}

        def _candidate_stats(cohort: Cohort) -> tuple[int, int, set[int]]:
            matched = {
                f_idx
                for f_idx in unassigned_cells
                if cohort.matches(ingestor.feat_idx_to_dict[f_idx])
            }
            leads_cnt = sum(cell_leads[i] for i in matched)
            conn_cnt = sum(cell_conn[i] for i in matched)
            return leads_cnt, conn_cnt, matched

        cohorts: list[Cohort] = []
        languages = [str(x) for x in wf_meta.get("languages", [])]

        # 1. Check specific high-signal 2-way cohorts first (evaluated in order before general 1-way cohorts)
        # 1a. Seniors (60+) in specific regional languages (e.g., senior_ta, senior_kn)
        for lang in languages:
            if lang in ("ta", "kn"):
                cand = Cohort(id=f"senior_{lang}", where={"age_band": ["60+"], "lang": [lang]})
                l_cnt, c_cnt, matched = _candidate_stats(cand)
                if c_cnt >= max(80, min_conn // 2) and l_cnt >= max(100, min_leads // 2):
                    cohorts.append(cand)
                    unassigned_cells -= matched

        # 2. General Senior cohort (60+)
        cand_senior = Cohort(id="senior_60plus", where={"age_band": ["60+"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_senior)
        if c_cnt >= max(80, min_conn // 2):
            cohorts.append(cand_senior)
            unassigned_cells -= matched

        # 3. Young (18-30) split by Metro (city_tier=1) vs Rest
        cand_young_t1 = Cohort(id="young_18_30_tier1", where={"age_band": ["18-30"], "city_tier": ["1"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_young_t1)
        if c_cnt >= min_conn and l_cnt >= min_leads:
            cohorts.append(cand_young_t1)
            unassigned_cells -= matched

        cand_young = Cohort(id="young_18_30", where={"age_band": ["18-30"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_young)
        if c_cnt >= max(100, min_conn // 2):
            cohorts.append(cand_young)
            unassigned_cells -= matched

        # 4. Mature (46-60) split by Metro (city_tier=1) vs Rest
        cand_mat_t1 = Cohort(id="mature_46_60_tier1", where={"age_band": ["46-60"], "city_tier": ["1"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_mat_t1)
        if c_cnt >= min_conn and l_cnt >= min_leads:
            cohorts.append(cand_mat_t1)
            unassigned_cells -= matched

        cand_mat = Cohort(id="mature_46_60", where={"age_band": ["46-60"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_mat)
        if c_cnt >= max(100, min_conn // 2):
            cohorts.append(cand_mat)
            unassigned_cells -= matched

        # 5. Mid-age (31-45) split by Metro (city_tier=1) while leaving remaining 31-45 as 'default'
        # so 'default' always has a healthy traffic share (>= 15-20% of workflow)
        cand_mid_t1 = Cohort(id="mid_31_45_tier1", where={"age_band": ["31-45"], "city_tier": ["1"]})
        l_cnt, c_cnt, matched = _candidate_stats(cand_mid_t1)
        remaining_after = sum(cell_leads[i] for i in (unassigned_cells - matched))
        if c_cnt >= min_conn and l_cnt >= min_leads and remaining_after >= min_leads:
            cohorts.append(cand_mid_t1)
            unassigned_cells -= matched

        return cohorts[: self.cfg.max_cohorts]

    def map_cells_to_cohorts(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        cohorts: list[Cohort],
    ) -> dict[int, str]:
        """Map every interned `feat_idx` for a workflow to its assigned cohort ID (or 'default')."""
        acc = ingestor.get_workflow(workflow_id)
        all_f_idxs = set(acc.history_lead_counts.keys()) | set(acc.live_lead_counts.keys())
        for f_idx, _ in acc.history_hour_pickup.keys():
            all_f_idxs.add(f_idx)
        for f_idx, _ in acc.live_hour_pickup.keys():
            all_f_idxs.add(f_idx)

        mapping: dict[int, str] = {0: "default"}
        for f_idx in all_f_idxs:
            if f_idx == 0:
                continue
            feats = ingestor.feat_idx_to_dict[f_idx]
            mapping[f_idx] = assign_cohort(cohorts, feats)
        return mapping

    def compute_hour_weights(
        self,
        ingestor: EventIngestor,
        workflow_id: str,
        wf_meta: dict[str, Any],
        cohorts: list[Cohort],
    ) -> dict[str, dict[str, float]]:
        """Compute smoothed per-cohort `schedule.hour_weights` strictly within `dialling_window` (C4).
        Never omits an in-window hour and never sets weight <= 0 so capacity is never wasted.
        """
        acc = ingestor.get_workflow(workflow_id)
        start_hr, end_hr = int(wf_meta["dialling_window"][0]), int(wf_meta["dialling_window"][1])
        valid_hours = list(range(start_hr, end_hr))

        cell_to_cid = self.map_cells_to_cohorts(ingestor, workflow_id, cohorts)
        cohort_ids = ["default"] + [c.id for c in cohorts]

        # Global hourly pickup across workflow
        wf_hour_stats: dict[int, list[int]] = {h: [0, 0] for h in valid_hours}
        # Per-cohort hourly pickup
        cid_hour_stats: dict[str, dict[int, list[int]]] = {
            cid: {h: [0, 0] for h in valid_hours} for cid in cohort_ids
        }

        for source in (acc.history_hour_pickup, acc.live_hour_pickup):
            for (f_idx, hr), (dials, conn) in source.items():
                if hr not in wf_hour_stats:
                    continue
                wf_hour_stats[hr][0] += dials
                wf_hour_stats[hr][1] += conn
                cid = cell_to_cid.get(f_idx, "default")
                if cid in cid_hour_stats:
                    cid_hour_stats[cid][hr][0] += dials
                    cid_hour_stats[cid][hr][1] += conn

        tot_wf_dials = sum(v[0] for v in wf_hour_stats.values())
        tot_wf_conn = sum(v[1] for v in wf_hour_stats.values())
        global_mean_p = tot_wf_conn / max(1, tot_wf_dials) if tot_wf_dials > 0 else 0.25

        wf_hour_rate: dict[int, float] = {}
        for h in valid_hours:
            d, c = wf_hour_stats[h]
            wf_hour_rate[h] = (c + 20.0 * global_mean_p) / (d + 20.0)

        prior_strength = self.cfg.hour_weight_smoothing_prior
        hour_weights: dict[str, dict[str, float]] = {}

        for cid in cohort_ids:
            rates: dict[int, float] = {}
            for h in valid_hours:
                d, c = cid_hour_stats[cid][h]
                rates[h] = (c + prior_strength * wf_hour_rate[h]) / (d + prior_strength)

            mean_r = sum(rates.values()) / max(1, len(rates))
            if mean_r <= 0:
                hour_weights[cid] = {str(h): 1.0 for h in valid_hours}
                continue

            # Rank hours for this cohort to accentuate top peak hours vs off-peak hours
            sorted_hrs = sorted(valid_hours, key=lambda h: rates[h], reverse=True)
            top_hrs = set(sorted_hrs[: max(2, len(valid_hours) // 3)])
            bottom_hrs = set(sorted_hrs[-max(2, len(valid_hours) // 3) :])

            cid_table: dict[str, float] = {}
            for h in valid_hours:
                rel = rates[h] / mean_r
                raw_w = rel ** self.cfg.hour_weight_exponent
                if h in top_hrs and rel >= 1.02:
                    raw_w *= 1.45
                elif h in bottom_hrs and rel <= 0.98:
                    raw_w *= 0.65
                clamped = max(self.cfg.hour_weight_min, min(self.cfg.hour_weight_max, raw_w))
                cid_table[str(h)] = round(clamped, 2)

            hour_weights[cid] = cid_table

        return hour_weights
