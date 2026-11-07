"""CLI entrypoint for the Outcome-First Controller (`python -m controller --url <SIM_URL>`)."""
import argparse
import sys

from controller.engine import ControllerEngine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Formi Outcome-First Agent Loop Controller")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080",
        help="Base HTTP URL of the simulator (e.g. http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--db",
        default="controller_state.db",
        help="Path to SQLite WAL audit & checkpoint database",
    )
    parser.add_argument(
        "--kill-at-minute",
        type=int,
        default=None,
        help="Optional simulation minute to trigger an immediate hard crash (os._exit) for recovery testing",
    )
    args = parser.parse_args(argv)

    engine = ControllerEngine(
        sim_url=args.url,
        db_path=args.db,
        kill_at_minute=args.kill_at_minute,
    )
    try:
        final_state = engine.run()
        print(
            f"[controller] Completed scenario={final_state.get('scenario')} "
            f"at sim_minute={final_state.get('sim_minute')} ({final_state.get('ts')}) "
            f"credits_remaining={final_state.get('credits_remaining')}"
        )
    finally:
        engine.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
