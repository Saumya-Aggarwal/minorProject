"""Run every test suite and the retrieval eval, one after another.

The suites share one Postgres database and each cleans up its own rows, so
they run in sequence, never in parallel. Output is kept quiet: one line per
suite, and the full output only for a suite that failed.

Needs Postgres and ChromaDB running (docker compose up -d).

Run from the repo root:
    backend/.venv/Scripts/python scripts/run_all_tests.py            all suites
    backend/.venv/Scripts/python scripts/run_all_tests.py cart live  only these
    backend/.venv/Scripts/python scripts/run_all_tests.py -v         stream everything
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

PY = sys.executable
SUITES = [
    ("cart", [PY, "scripts/test_cart.py"]),
    ("assistant", [PY, "scripts/test_assistant.py"]),
    ("payments", [PY, "scripts/test_payments.py"]),
    ("store", [PY, "scripts/test_store.py"]),
    ("training", [PY, "scripts/test_training.py"]),
    ("live", [PY, "scripts/test_live.py"]),
    ("linking", [PY, "scripts/test_linking.py"]),
    ("live.js", ["node", "scripts/test_live_js.mjs"]),
    ("retrieval", [PY, "scripts/eval_retrieval.py"]),
]


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _services_down() -> list[str]:
    db = urlparse(os.environ.get("DATABASE_URL", "postgresql://localhost:5432"))
    services = [("Postgres", db.hostname or "localhost", db.port or 5432),
                ("ChromaDB", os.environ.get("CHROMA_HOST", "localhost"),
                 int(os.environ.get("CHROMA_PORT", "8001")))]
    return [f"{name} ({host}:{port})" for name, host, port in services
            if not _reachable(host, port)]


def _summary(output: str) -> str:
    """The suite's last non-empty line, e.g. '126 passed, 0 failed'."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:90] if lines else ""


def main() -> int:
    verbose = "-v" in sys.argv[1:]
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    unknown = set(wanted) - {name for name, _ in SUITES}
    if unknown:
        print(f"Unknown suite(s): {', '.join(sorted(unknown))}. "
              f"Choose from: {', '.join(name for name, _ in SUITES)}")
        return 2
    suites = [(n, cmd) for n, cmd in SUITES if not wanted or n in wanted]

    down = _services_down()
    if down:
        print(f"Not reachable: {', '.join(down)}. Start them with: docker compose up -d")
        return 2
    if shutil.which("node") is None:
        suites = [(n, cmd) for n, cmd in suites if cmd[0] != "node"]
        print("node not found: skipping live.js")

    # Children print Rs signs too; make sure their stdout is UTF-8 whatever the pipe
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    failed = []
    started = time.monotonic()
    for name, cmd in suites:
        t0 = time.monotonic()
        if verbose:
            print(f"\n===== {name} =====", flush=True)
            code = subprocess.run(cmd, cwd=ROOT, env=env).returncode
            output = ""
        else:
            run = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace")
            code, output = run.returncode, run.stdout + run.stderr
        mark = "PASS" if code == 0 else "FAIL"
        print(f"{mark}  {name:<10} {time.monotonic() - t0:5.1f}s  {_summary(output)}", flush=True)
        if code != 0:
            failed.append(name)
            if output:
                print(f"----- {name} output -----\n{output.rstrip()}\n----- end {name} -----")

    total = time.monotonic() - started
    if failed:
        print(f"\n{len(failed)} of {len(suites)} suites failed ({', '.join(failed)}) in {total:.0f}s")
        return 1
    print(f"\nAll {len(suites)} suites passed in {total:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
