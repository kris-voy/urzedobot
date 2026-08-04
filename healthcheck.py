"""Docker healthcheck: paused is alive; degraded/unavailable is unhealthy."""
from svctl import EXIT_OK, EXIT_PAUSED, doctor_report
from state import StateStore
import os


if __name__ == "__main__":
    code, _ = doctor_report(StateStore(os.environ.get("DATABASE_PATH", "/app/data/sv.db")))
    raise SystemExit(0 if code in {EXIT_OK, EXIT_PAUSED} else 1)
