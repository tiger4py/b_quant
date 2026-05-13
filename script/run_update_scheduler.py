"""Project-local scheduler for running update_daily.py without agent support."""
import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = BASE_DIR / "script" / "update_daily.py"
STATE_FILE = Path(DATA_DIR) / "update_scheduler_state.json"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("load state failed: %s", exc)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_weekdays(value: str) -> set[int]:
    items = {int(part.strip()) for part in value.split(",") if part.strip()}
    invalid = {item for item in items if item < 0 or item > 6}
    if invalid:
        raise ValueError(f"invalid weekday values: {sorted(invalid)}")
    return items


def should_run_today(
    now: datetime,
    hour: int,
    minute: int,
    weekdays: set[int],
    state: dict,
    retry_minutes: int,
) -> bool:
    if now.weekday() not in weekdays:
        return False

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return False

    today = now.strftime("%Y-%m-%d")
    if state.get("last_success_date") == today:
        return False

    last_attempt_at = state.get("last_attempt_at")
    if last_attempt_at:
        try:
            attempt_time = datetime.fromisoformat(last_attempt_at)
            if attempt_time.date() == now.date() and now - attempt_time < timedelta(minutes=retry_minutes):
                return False
        except ValueError:
            pass

    return True


def run_update_job(state: dict) -> int:
    now = datetime.now()
    state["last_attempt_at"] = now.isoformat(timespec="seconds")
    save_state(state)

    logger.info("starting daily update: %s", UPDATE_SCRIPT)
    result = subprocess.run([sys.executable, str(UPDATE_SCRIPT)], cwd=str(BASE_DIR))

    state["last_return_code"] = result.returncode
    state["last_finished_at"] = datetime.now().isoformat(timespec="seconds")
    if result.returncode == 0:
        state["last_success_date"] = now.strftime("%Y-%m-%d")
        logger.info("daily update finished successfully")
    else:
        logger.error("daily update failed with exit code %s", result.returncode)
    save_state(state)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run update_daily.py on a fixed daily schedule.")
    parser.add_argument("--hour", type=int, default=18, help="target hour, default 18")
    parser.add_argument("--minute", type=int, default=0, help="target minute, default 0")
    parser.add_argument(
        "--weekdays",
        default="0,1,2,3,4",
        help="comma-separated weekdays, Monday=0 ... Sunday=6; default weekdays only",
    )
    parser.add_argument("--poll-seconds", type=int, default=30, help="check interval, default 30")
    parser.add_argument("--retry-minutes", type=int, default=30, help="retry delay after failure, default 30")
    parser.add_argument("--once", action="store_true", help="run update once immediately and exit")
    args = parser.parse_args()

    weekdays = parse_weekdays(args.weekdays)
    state = load_state()

    if args.once:
        return run_update_job(state)

    logger.info(
        "scheduler started: run at %02d:%02d on weekdays=%s, poll=%ss",
        args.hour,
        args.minute,
        sorted(weekdays),
        args.poll_seconds,
    )
    logger.info("python scheduler mode; no agent required")

    try:
        while True:
            now = datetime.now()
            if should_run_today(now, args.hour, args.minute, weekdays, state, args.retry_minutes):
                run_update_job(state)
                state = load_state()
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("scheduler stopped by user")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
