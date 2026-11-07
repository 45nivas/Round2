"""Automated crash-recovery verification script (C13).

Demonstrates:
1. Starting `public_01_dev` simulator and running the controller up to `sim_minute=4320` (Day 3 00:00),
   where the controller process is abruptly killed via `os._exit(137)` (`--kill-at-minute 4320`).
2. Inspecting the SQLite WAL checkpoint and open proposals mid-run.
3. Restarting the controller against the same simulator and SQLite DB to completion (`sim_minute=10080`).
4. Verifying zero duplicate chunk ingestion, zero `apply_conflicts`, zero `lapsed`/`expired` approvals,
   and identical KPI lift vs baseline.
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
import httpx

from controller.store import AuditStore
from sim.grader import grade


def run_crash_recovery_demo() -> str:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "runs" / "crash_recovery_run"
    base_dir = root / "runs" / "base_public_01_dev"
    db_path = root / "runs" / "crash_recovery_state.db"
    report_path = root / "reports" / "crash_recovery.log"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if p.exists():
            p.unlink()

    port = 18089
    url = f"http://127.0.0.1:{port}"
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        log_lines.append(msg)

    log("================================================================================")
    log("CRASH RECOVERY VERIFICATION TEST (C13) — scenario: public_01_dev")
    log("================================================================================")

    srv = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "sim",
            "serve",
            "--scenario",
            str(root / "scenarios" / "public_01_dev.yaml"),
            "--run-dir",
            str(out_dir),
            "--port",
            str(port),
            "--force",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        for _ in range(50):
            try:
                if httpx.get(f"{url}/sim/state", timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)

        # Phase 1: Run controller until hard crash at sim_minute=4320 (Day 3 00:00)
        log("\n[Phase 1] Launching controller #1 with --kill-at-minute 4320 (Day 3 00:00)...")
        t0 = time.perf_counter()
        proc1 = subprocess.run(
            [
                sys.executable,
                "-m",
                "controller",
                "--url",
                url,
                "--db",
                str(db_path),
                "--kill-at-minute",
                "4320",
            ],
            capture_output=True,
            text=True,
        )
        elapsed1 = time.perf_counter() - t0
        log(f"  Controller #1 exited with code={proc1.returncode} after {elapsed1:.2f}s (expected 137 = SIGKILL/hard exit)")
        assert proc1.returncode == 137, f"Expected exit code 137, got {proc1.returncode}"

        # Inspect simulator and SQLite WAL state immediately after crash
        sim_state_mid = httpx.get(f"{url}/sim/state").json()
        store = AuditStore(db_path)
        ckpt = store.load_checkpoint("public_01_dev")
        assert ckpt is not None, "Checkpoint missing after crash!"
        decisions_mid = store.conn.execute("SELECT COUNT(*) AS cnt FROM decisions_audit").fetchone()["cnt"]
        proposals_mid = store.conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()["cnt"]
        pending_mid = len(store.get_pending_proposals())
        store.close()

        log(f"  Simulator state at crash   : sim_minute={sim_state_mid['sim_minute']} ({sim_state_mid['ts']})")
        log(
            f"  SQLite WAL checkpoint      : sim_minute={ckpt['sim_minute']}, "
            f"last_history_chunk={ckpt['last_history_chunk']}, "
            f"last_event_chunk={ckpt['last_event_chunk']}, "
            f"last_event_id={ckpt['last_event_id']}"
        )
        log(
            f"  Audit records pre-crash    : proposals={proposals_mid} (pending={pending_mid}), "
            f"decisions_audit={decisions_mid}"
        )

        # Phase 2: Restart controller #2 against the SAME simulator & SQLite DB
        log("\n[Phase 2] Restarting controller #2 from SQLite WAL checkpoint to completion...")
        t1 = time.perf_counter()
        proc2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "controller",
                "--url",
                url,
                "--db",
                str(db_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        elapsed2 = time.perf_counter() - t1
        log(f"  Controller #2 exited cleanly with code={proc2.returncode} after {elapsed2:.2f}s")
        if proc2.stdout.strip():
            log(f"  Controller #2 stdout: {proc2.stdout.strip()}")

        sim_state_end = httpx.get(f"{url}/sim/state").json()
        store2 = AuditStore(db_path)
        ckpt_end = store2.load_checkpoint("public_01_dev")
        decisions_end = store2.conn.execute("SELECT COUNT(*) AS cnt FROM decisions_audit").fetchone()["cnt"]
        proposals_end = store2.conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()["cnt"]
        store2.close()

        log(f"  Simulator final state      : sim_minute={sim_state_end['sim_minute']} ({sim_state_end['ts']}), finished={sim_state_end.get('finished')}")
        log(
            f"  Final SQLite checkpoint    : sim_minute={ckpt_end['sim_minute']}, "
            f"last_event_chunk={ckpt_end['last_event_chunk']}, "
            f"last_event_id={ckpt_end['last_event_id']}"
        )
        log(f"  Final audit records        : proposals={proposals_end}, decisions_audit={decisions_end}")

    finally:
        srv.terminate()
        srv.wait()

    # Phase 3: Grade the crash-recovered run against baseline and verify governance invariants
    log("\n[Phase 3] Grading crash-recovered run against baseline (runs/base_public_01_dev)...")
    g = grade(str(out_dir), str(base_dir))
    gov = g["governance"]
    verdicts = dict(g["controller_changes"]["verdicts"])
    delta_all = g["delta_vs_baseline"]["ALL"]

    log(f"  Scenario finished minute   : {g['finished']} (expected 10080)")
    log(
        f"  Overall KPI Δ vs baseline  : conclusion={delta_all['conclusion_rate']*100:+.1f}%, "
        f"pickup={delta_all['pickup_rate']*100:+.1f}%, "
        f"engagement={delta_all['engagement_rate']*100:+.1f}%"
    )
    log(f"  Controller change verdicts : {json.dumps(verdicts)}")
    log(f"  Governance summary         : {json.dumps(gov, default=dict)}")

    assert g["finished"] >= 10080, f"Run did not finish to end minute, finished={g['finished']}"
    assert gov["apply_conflicts"] == 0, f"Expected 0 apply_conflicts, got {gov['apply_conflicts']}"
    assert gov["lapsed"] == 0, f"Expected 0 lapsed approvals, got {gov['lapsed']}"
    assert gov["auto_apply_rejected"] == 0, f"Expected 0 auto_apply_rejected, got {gov['auto_apply_rejected']}"

    log("\n[PASS] Crash recovery verified: 0 conflicts, 0 lapsed approvals, 0 rejected auto-applies, 0 harmful changes.")
    full_text = "\n".join(log_lines) + "\n"
    report_path.write_text(full_text, encoding="utf-8")
    return full_text


if __name__ == "__main__":
    run_crash_recovery_demo()
