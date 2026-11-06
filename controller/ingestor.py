"""High-throughput streaming NDJSON event ingestor with feature-tuple interning (C1, C2, C3)."""
from collections import deque
import json
import time
from typing import Any
import httpx

from controller.config import ControllerConfig, DEFAULT_CONFIG


def _extract_lead_num(lead_id: str) -> int:
    """Fast extraction of integer lead index from '<workflow_id>:l<digits>', or -1 if non-standard."""
    idx = lead_id.rfind(":l")
    if idx != -1:
        suffix = lead_id[idx + 2 :]
        if suffix.isdigit():
            return int(suffix)
    return -1


class WorkflowAccumulator:
    """Compact per-workflow statistical accumulators and sliding windows."""

    def __init__(self, workflow_id: str, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.workflow_id = workflow_id
        self.cfg = cfg

        # Compact lead_id -> feat_idx mapping: bytearray for numeric ':l<int>' IDs + fallback dict
        self.lead_feat_array = bytearray(65536)
        self.lead_feat_fallback: dict[str, int] = {}

        # Feature cell counts
        self.history_lead_counts: dict[int, int] = {}
        self.live_lead_counts: dict[int, int] = {}
        self.recent_campaign_sizes: deque[int] = deque(maxlen=14)

        # Call-time pickup stats: (feat_idx, hour) -> [dials, connected]
        self.history_hour_pickup: dict[tuple[int, int], list[int]] = {}
        self.live_hour_pickup: dict[tuple[int, int], list[int]] = {}

        # Overall KPIs: [dials, connected, engaged, hallucinated]
        self.history_totals: list[int] = [0, 0, 0, 0]
        self.live_totals: list[int] = [0, 0, 0, 0]
        self.recent_live_calls: deque[tuple[int, int, int]] = deque(maxlen=400)  # (connected, engaged, halluc)

        # Variant stats per feature cell: (feat_idx, dim, opt) -> [connected, engaged]
        self.history_cell_variants: dict[tuple[int, str, str], list[int]] = {}
        self.live_cell_variants: dict[tuple[int, str, str], list[int]] = {}

        # Global workflow variant stats: (dim, opt) -> [connected, engaged]
        self.history_global_variants: dict[tuple[str, str], list[int]] = {}
        self.live_global_variants: dict[tuple[str, str], list[int]] = {}

        # Variant guardrails (pickup rate by welcome_message/voice): (dim, opt) -> [dials, connected]
        self.live_variant_pickup: dict[tuple[str, str], list[int]] = {}

        # Variant decay tracking (C7): (dim, opt) -> sliding window of 0/1 engaged on connected calls
        self.variant_recent_window: dict[tuple[str, str], deque[int]] = {}
        self.variant_peak_rate: dict[tuple[str, str], float] = {}
        self.variant_peak_samples: dict[tuple[str, str], int] = {}
        self.decayed_variants: set[tuple[str, str]] = set()

        # Caller-ID health tracking (C5)
        self.history_did_pickup: dict[str, list[int]] = {}  # did -> [dials, connected]
        self.live_did_pickup: dict[str, list[int]] = {}     # did -> [dials, connected]
        self.live_did_engagement: dict[str, list[int]] = {} # did -> [connected, engaged]
        self.did_recent_window: dict[str, deque[int]] = {}  # did -> deque of 0/1 connected
        self.degraded_dids: set[str] = set()

        # Prompt & hallucination tracking (C8)
        self.active_prompt_version: str = "P1"
        self.prompt_epoch_stats: dict[str, list[int]] = {}  # prompt -> [connected, engaged, hallucinated]
        self.prompt_recent_halluc: dict[str, deque[int]] = {}  # prompt -> deque of 0/1 halluc
        self.prompt_changed_at_minute: int = 0

        # Supervisor & governance event tracking
        self.last_supervisor_edit_minute: int | None = None
        self.last_supervisor_edit_ts: str | None = None
        self.last_supervisor_edit_dims: list[str] = []
        self.last_config_version: int = 1

    def set_lead_feat(self, lead_id: str, feat_idx: int) -> None:
        num = _extract_lead_num(lead_id)
        if num >= 0:
            if num >= len(self.lead_feat_array):
                new_size = max(num + 1, len(self.lead_feat_array) * 2)
                self.lead_feat_array.extend( b"\x00" * (new_size - len(self.lead_feat_array)) )
            self.lead_feat_array[num] = feat_idx
        else:
            self.lead_feat_fallback[lead_id] = feat_idx

    def get_lead_feat(self, lead_id: str) -> int:
        num = _extract_lead_num(lead_id)
        if num >= 0:
            if num < len(self.lead_feat_array):
                return self.lead_feat_array[num]
            return 0
        return self.lead_feat_fallback.get(lead_id, 0)

    def reset_prompt_epoch(self, new_prompt: str, sim_minute: int) -> None:
        self.active_prompt_version = new_prompt
        self.prompt_epoch_stats[new_prompt] = [0, 0, 0]
        self.prompt_recent_halluc[new_prompt] = deque(maxlen=150)
        self.prompt_changed_at_minute = sim_minute


class EventIngestor:
    """Streaming, idempotent, restartable NDJSON chunk ingestor (C1)."""

    def __init__(self, cfg: ControllerConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        # Intern table for feature dicts: index 0 is empty dict {}
        self.feat_idx_to_dict: list[dict[str, str]] = [{}]
        self.feat_tuple_to_idx: dict[tuple[tuple[str, str], ...], int] = {(): 0}

        self.workflows: dict[str, WorkflowAccumulator] = {}
        self.last_history_chunk: int = -1
        self.last_event_chunk: int = -1
        self.last_event_id: int = 0
        self.total_events_ingested: int = 0

        # Transient event queues drained by the controller loop each tick
        self.pending_config_events: list[dict[str, Any]] = []
        self.pending_approval_events: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> WorkflowAccumulator:
        acc = self.workflows.get(workflow_id)
        if acc is None:
            acc = WorkflowAccumulator(workflow_id, self.cfg)
            self.workflows[workflow_id] = acc
        return acc

    def intern_features(self, features: dict[str, str]) -> int:
        key = tuple(sorted((str(k), str(v)) for k, v in features.items()))
        idx = self.feat_tuple_to_idx.get(key)
        if idx is None:
            idx = len(self.feat_idx_to_dict)
            if idx >= 255:
                return 0
            self.feat_idx_to_dict.append(dict(key))
            self.feat_tuple_to_idx[key] = idx
        return idx

    def export_state(self) -> dict[str, Any]:
        return {
            "feat_idx_to_dict": self.feat_idx_to_dict,
            "feat_tuple_to_idx": self.feat_tuple_to_idx,
            "workflows": self.workflows,
            "last_history_chunk": self.last_history_chunk,
            "last_event_chunk": self.last_event_chunk,
            "last_event_id": self.last_event_id,
            "total_events_ingested": self.total_events_ingested,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self.feat_idx_to_dict = state["feat_idx_to_dict"]
        self.feat_tuple_to_idx = state["feat_tuple_to_idx"]
        self.workflows = state["workflows"]
        self.last_history_chunk = state["last_history_chunk"]
        self.last_event_chunk = state["last_event_chunk"]
        self.last_event_id = state["last_event_id"]
        self.total_events_ingested = state["total_events_ingested"]

    def ingest_available_chunks(
        self,
        client: httpx.Client,
        kind: str = "events",
        chunk_Descriptors: list[dict[str, Any]] | None = None,
    ) -> tuple[int, float]:
        """Fetch and stream all unseen chunks of `kind` ('history' or 'events').
        Returns (events_processed, elapsed_seconds).
        """
        t0 = time.perf_counter()
        after = self.last_history_chunk if kind == "history" else self.last_event_chunk
        if chunk_Descriptors is None:
            resp = client.get("/events/chunks", params={"kind": kind, "after": after})
            resp.raise_for_status()
            chunk_Descriptors = resp.json()

        events_count = 0
        for ch in sorted(chunk_Descriptors, key=lambda x: int(x["chunk"])):
            c_num = int(ch["chunk"])
            if kind == "history" and c_num <= self.last_history_chunk:
                continue
            if kind == "events" and c_num <= self.last_event_chunk:
                continue

            events_count += self._stream_single_chunk(client, c_num, is_history=(kind == "history"))
            if kind == "history":
                self.last_history_chunk = c_num
            else:
                self.last_event_chunk = c_num

        elapsed = max(1e-6, time.perf_counter() - t0)
        return events_count, elapsed

    def _stream_single_chunk(self, client: httpx.Client, chunk_num: int, is_history: bool) -> int:
        count = 0
        decay_win_len = self.cfg.decay_window_calls
        did_win_len = self.cfg.did_window_dials

        with client.stream("GET", f"/events/chunks/{chunk_num}") as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                ev = json.loads(line)
                ev_id = int(ev.get("event_id", 0))
                if ev_id <= self.last_event_id:
                    continue
                self.last_event_id = ev_id
                self.total_events_ingested += 1
                count += 1

                ev_type = ev["type"]
                if ev_type == "call":
                    wid = ev["workflow_id"]
                    acc = self.get_workflow(wid)
                    feat_idx = acc.get_lead_feat(ev["lead_id"])
                    hr = int(ev["ts"][11:13])
                    conn = 1 if ev["connected"] else 0
                    eng = 1 if (conn and ev.get("engaged")) else 0
                    halluc = 1 if (conn and ev.get("qa", {}).get("hallucination_flag")) else 0

                    variant = ev["variant"]
                    wm = variant["welcome_message"]
                    vc = variant["voice"]
                    did = variant["caller_id"]
                    pv = variant["prompt_version"]

                    if is_history:
                        t = acc.history_totals
                        t[0] += 1
                        t[1] += conn
                        t[2] += eng
                        t[3] += halluc

                        hp = acc.history_hour_pickup.get((feat_idx, hr))
                        if hp is None:
                            acc.history_hour_pickup[(feat_idx, hr)] = [1, conn]
                        else:
                            hp[0] += 1
                            hp[1] += conn

                        dp = acc.history_did_pickup.get(did)
                        if dp is None:
                            acc.history_did_pickup[did] = [1, conn]
                        else:
                            dp[0] += 1
                            dp[1] += conn

                        if conn:
                            for dim, opt in (("welcome_message", wm), ("voice", vc)):
                                gv = acc.history_global_variants.get((dim, opt))
                                if gv is None:
                                    acc.history_global_variants[(dim, opt)] = [1, eng]
                                else:
                                    gv[0] += 1
                                    gv[1] += eng

                                cv = acc.history_cell_variants.get((feat_idx, dim, opt))
                                if cv is None:
                                    acc.history_cell_variants[(feat_idx, dim, opt)] = [1, eng]
                                else:
                                    cv[0] += 1
                                    cv[1] += eng
                    else:
                        t = acc.live_totals
                        t[0] += 1
                        t[1] += conn
                        t[2] += eng
                        t[3] += halluc
                        acc.recent_live_calls.append((conn, eng, halluc))

                        hp = acc.live_hour_pickup.get((feat_idx, hr))
                        if hp is None:
                            acc.live_hour_pickup[(feat_idx, hr)] = [1, conn]
                        else:
                            hp[0] += 1
                            hp[1] += conn

                        dp = acc.live_did_pickup.get(did)
                        if dp is None:
                            acc.live_did_pickup[did] = [1, conn]
                        else:
                            dp[0] += 1
                            dp[1] += conn

                        dw = acc.did_recent_window.get(did)
                        if dw is None:
                            dw = deque(maxlen=did_win_len)
                            acc.did_recent_window[did] = dw
                        dw.append(conn)

                        for dim, opt in (("welcome_message", wm), ("voice", vc)):
                            vp = acc.live_variant_pickup.get((dim, opt))
                            if vp is None:
                                acc.live_variant_pickup[(dim, opt)] = [1, conn]
                            else:
                                vp[0] += 1
                                vp[1] += conn

                        if conn:
                            de = acc.live_did_engagement.get(did)
                            if de is None:
                                acc.live_did_engagement[did] = [1, eng]
                            else:
                                de[0] += 1
                                de[1] += eng

                            for dim, opt in (("welcome_message", wm), ("voice", vc)):
                                key = (dim, opt)
                                gv = acc.live_global_variants.get(key)
                                if gv is None:
                                    gv = [1, eng]
                                    acc.live_global_variants[key] = gv
                                else:
                                    gv[0] += 1
                                    gv[1] += eng

                                cv = acc.live_cell_variants.get((feat_idx, dim, opt))
                                if cv is None:
                                    acc.live_cell_variants[(feat_idx, dim, opt)] = [1, eng]
                                else:
                                    cv[0] += 1
                                    cv[1] += eng

                                vw = acc.variant_recent_window.get(key)
                                if vw is None:
                                    vw = deque(maxlen=decay_win_len)
                                    acc.variant_recent_window[key] = vw
                                vw.append(eng)

                                # Update peak rolling rate once we have a solid sample (>= 90 calls)
                                if len(vw) >= 90:
                                    cur_rate = sum(vw) / len(vw)
                                    prev_peak = acc.variant_peak_rate.get(key, 0.0)
                                    if cur_rate > prev_peak:
                                        acc.variant_peak_rate[key] = cur_rate
                                        acc.variant_peak_samples[key] = len(vw)

                            # Prompt version tracking
                            ps = acc.prompt_epoch_stats.get(pv)
                            if ps is None:
                                ps = [0, 0, 0]
                                acc.prompt_epoch_stats[pv] = ps
                            ps[0] += 1
                            ps[1] += eng
                            ps[2] += halluc

                            ph = acc.prompt_recent_halluc.get(pv)
                            if ph is None:
                                ph = deque(maxlen=150)
                                acc.prompt_recent_halluc[pv] = ph
                            ph.append(halluc)

                elif ev_type == "lead":
                    wid = ev["workflow_id"]
                    acc = self.get_workflow(wid)
                    feat_idx = self.intern_features(ev["features"])
                    acc.set_lead_feat(ev["lead_id"], feat_idx)
                    if is_history:
                        acc.history_lead_counts[feat_idx] = acc.history_lead_counts.get(feat_idx, 0) + 1
                    else:
                        acc.live_lead_counts[feat_idx] = acc.live_lead_counts.get(feat_idx, 0) + 1

                elif ev_type == "campaign_created":
                    wid = ev["workflow_id"]
                    acc = self.get_workflow(wid)
                    acc.recent_campaign_sizes.append(int(ev.get("leads", 0)))

                elif ev_type == "config_changed":
                    wid = ev["workflow_id"]
                    acc = self.get_workflow(wid)
                    acc.last_config_version = int(ev.get("version", acc.last_config_version))
                    if not is_history:
                        self.pending_config_events.append(ev)

                elif ev_type == "approval_decided":
                    if not is_history:
                        self.pending_approval_events.append(ev)

        return count
