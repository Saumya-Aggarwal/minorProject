"""Export the logged bot replies and the owner/customer ratings to evals/raw/.

P2's eval harness is built from these conversations, and P1's schema changes
drop tables, so this snapshot is the copy that survives. Plain SQL rather than
the SQLModel classes, so it still runs after models.py changes.

evals/raw/ is gitignored: these are customers' own messages, and the repo is
public. Keep the folder backed up by hand (it sits beside backups/).

The question embeddings on feedback are left out: they are recomputed from
`question` by whichever embedder is current (P1 replaces MiniLM anyway).

Run from the repo root:  backend/.venv/Scripts/python scripts/export_training_data.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from sqlalchemy import text  # noqa: E402

from db import get_engine  # noqa: E402

OUT = ROOT / "evals" / "raw"

QUERIES = {
    "bot_replies": """
        SELECT reply_id, user_id, question, answer, product_ids, path, created_at
        FROM bot_replies ORDER BY reply_id""",
    "feedback": """
        SELECT feedback_id, scope, rating, product_id, question, note, reply_id, user_id, created_at
        FROM feedback ORDER BY feedback_id""",
}


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    counts = {}
    with get_engine().connect() as conn:
        for table, sql in QUERIES.items():
            rows = [{k: _plain(v) for k, v in row._mapping.items()} for row in conn.execute(text(sql))]
            (OUT / f"{table}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            counts[table] = len(rows)
            print(f"{table}: {len(rows)} rows -> {(OUT / f'{table}.json').relative_to(ROOT)}")
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "omitted": {"feedback.embedding": "recompute from question with the current embedder"},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
