"""Cohort discovery from history and comparative-advantage call-time schedule optimization (C2, C3, C4)."""
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
        """Discover a stable, non-fragmented ordered list of shared Cohort objects (C2, C3)
        that simultaneously captures age_band call-time archetypes, language segments, and city_tier=1.
        """
        acc = ingestor.get_workflow(workflow_id)

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
        min_leads = max(120, int(0.025 * total_leads))
        unassigned_cells = {f_idx for f_idx in cell_leads if f_idx != 0}

        def _try_add(cohorts_list: list[Cohort], cid: str, where: dict[str, list[str]]) -> None:
            if len(cohorts_list) >= 16:
                return
            cand = Cohort(id=cid, where=where)
            matched = {
                f_idx
                for f_idx in unassigned_cells
                if cand.matches(ingestor.feat_idx_to_dict[f_idx])
            }
            leads_cnt = sum(cell_leads[i] for i in matched)
            if leads_cnt >= min_leads:
                cohorts_list.append(cand)
                unassigned_cells.difference_update(matched)

        cohorts: list[Cohort] = []
        languages = [str(x) for x in wf_meta.get("languages", [])]
        # Order secondary languages first so specific language effects are matched cleanly
        non_en_langs = [l for l in languages if l != "en"]
        primary_langs = non_en_langs + (["en"] if "en" in languages else [])

        # 1. Seniors (60+) — strong morning pickup archetype, partitioned by language
        for lang in non_en_langs:
            _try_add(cohorts, f"senior_60_{lang}", {"age_band": ["60+"], "lang": [lang]})
        _try_add(cohorts, "senior_60_tier1", {"age_band": ["60+"], "city_tier": ["1"]})
        _try_add(cohorts, "senior_60_rest", {"age_band": ["60+"]})

        # 2. Mature (46-60) — morning/lunch pickup archetype, partitioned by language & tier 1
        for lang in non_en_langs:
            _try_add(cohorts, f"mature_46_60_{lang}", {"age_band": ["46-60"], "lang": [lang]})
        _try_add(cohorts, "mature_46_60_tier1", {"age_band": ["46-60"], "city_tier": ["1"]})
        _try_add(cohorts, "mature_46_60_rest", {"age_band": ["46-60"]})

        # 3. Young (18-30) — strong evening pickup archetype, partitioned by language & tier 1
        for lang in non_en_langs:
            _try_add(cohorts, f"young_18_30_{lang}", {"age_band": ["18-30"], "lang": [lang]})
        _try_add(cohorts, "young_18_30_tier1", {"age_band": ["18-30"], "city_tier": ["1"]})
        _try_add(cohorts, "young_18_30_rest", {"age_band": ["18-30"]})

        # 4. Mid-age (31-45) — lunch/evening archetype, partitioned by non-en language & tier 1
        # Leaving 31-45 English tier 2/3 as 'default' so 'default' always has a solid lead share
        for lang in non_en_langs:
            _try_add(cohorts, f"mid_31_45_{lang}", {"age_band": ["31-45"], "lang": [lang]})
        _try_add(cohorts, "mid_31_45_tier1", {"age_band": ["31-45"], "city_tier": ["1"]})

        return cohorts[:16]

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
        """Compute comparative-advantage per-cohort `schedule.hour_weights` within `dialling_window` (C4).
        Pools across cohorts sharing the same `age_band` for high statistical precision, and separates
        morning, lunch, and evening cohorts so the Gumbel dialler allocates each hour's capacity to
        the cohort whose pickup peaks in that hour.
        """
        acc = ingestor.get_workflow(workflow_id)
        start_hr, end_hr = int(wf_meta["dialling_window"][0]), int(wf_meta["dialling_window"][1])
        valid_hours = list(range(start_hr, end_hr))

        cell_to_cid = self.map_cells_to_cohorts(ingestor, workflow_id, cohorts)
        cohort_ids = ["default"] + [c.id for c in cohorts]
        cid_to_cohort = {c.id: c for c in cohorts}

        wf_hour_stats: dict[int, list[int]] = {h: [0, 0] for h in valid_hours}
        cid_hour_stats: dict[str, dict[int, list[int]]] = {
            cid: {h: [0, 0] for h in valid_hours} for cid in cohort_ids
        }
        age_hour_stats: dict[str, dict[int, list[int]]] = defaultdict(
            lambda: {h: [0, 0] for h in valid_hours}
        )

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
                if f_idx != 0:
                    ab = ingestor.feat_idx_to_dict[f_idx].get("age_band", "")
                    if ab:
                        age_hour_stats[ab][hr][0] += dials
                        age_hour_stats[ab][hr][1] += conn

        tot_wf_dials = sum(v[0] for v in wf_hour_stats.values())
        tot_wf_conn = sum(v[1] for v in wf_hour_stats.values())
        global_mean_p = tot_wf_conn / max(1, tot_wf_dials) if tot_wf_dials > 0 else 0.25

        wf_hour_rate: dict[int, float] = {}
        for h in valid_hours:
            d, c = wf_hour_stats[h]
            wf_hour_rate[h] = (c + 20.0 * global_mean_p) / (d + 20.0)

        hour_weights: dict[str, dict[str, float]] = {}

        for cid in cohort_ids:
            c_obj = cid_to_cohort.get(cid)
            ab_list = c_obj.where.get("age_band", []) if c_obj else ["31-45"]
            ab_key = ab_list[0] if len(ab_list) == 1 else ""

            rates: dict[int, float] = {}
            for h in valid_hours:
                d_c, c_c = cid_hour_stats[cid][h]
                if ab_key and ab_key in age_hour_stats:
                    d_a, c_a = age_hour_stats[ab_key][h]
                    age_r = (c_a + 25.0 * wf_hour_rate[h]) / (d_a + 25.0)
                else:
                    age_r = wf_hour_rate[h]
                rates[h] = (c_c + 45.0 * age_r) / (d_c + 45.0)

            mean_r = sum(rates.values()) / max(1, len(rates))
            if mean_r <= 0:
                hour_weights[cid] = {str(h): 1.0 for h in valid_hours}
                continue

            # Comparative advantage score: cohort's relative hour rate vs workflow relative hour rate
            comp_adv: dict[int, float] = {}
            for h in valid_hours:
                rel_cid = rates[h] / mean_r
                rel_wf = wf_hour_rate[h] / max(1e-6, global_mean_p)
                # Blend absolute hourly pickup (65%) and comparative advantage vs other cohorts (35%)
                comp_adv[h] = (rel_cid ** 2.2) * ((rel_cid / max(0.5, rel_wf)) ** 1.4)

            sorted_hrs = sorted(valid_hours, key=lambda h: comp_adv[h], reverse=True)
            n_hrs = len(valid_hours)
            top_k = set(sorted_hrs[: max(3, n_hrs // 3)])
            bot_k = set(sorted_hrs[-max(3, n_hrs // 3) :])

            cid_table: dict[str, float] = {}
            for h in valid_hours:
                w = comp_adv[h]
                if h in top_k and rates[h] >= mean_r:
                    w *= 2.8
                elif h in bot_k and rates[h] <= mean_r:
                    w *= 0.22
                clamped = max(0.10, min(9.50, w))
                cid_table[str(h)] = round(clamped, 2)

            hour_weights[cid] = cid_table

        return hour_weights
