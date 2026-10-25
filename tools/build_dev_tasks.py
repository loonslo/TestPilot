"""Write the Agent-facing task files from the developer's manifest.

The manifest says which build each task runs on; the task files must not. They only carry the
scope (rules, operations, callers) and the limits. Run from the project root:

    uv run python tools/build_dev_tasks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "dev" / "manifest.json"
OUTPUT = ROOT / "benchmarks" / "public" / "tasks"

BRIEF = (
    "You are testing part of a personal bookkeeping service: accounts, holdings, transactions and "
    "a ledger summary. Learn the interface, prepare your own data with the allowed operations, "
    "observe the responses, and judge the service's behaviour against the listed rules. Submit a "
    "replayable case for every behaviour that breaks a rule. If you find nothing, say what you "
    "checked. Use only the listed operations and callers."
)
LIMITS = {"max_model_calls": 12, "max_http_calls": 30, "max_seconds": 600}


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for stale in OUTPUT.glob("dev-*.json"):
        stale.unlink()
    for task in manifest["tasks"]:
        scope = manifest["scopes"][task["scope"]]
        payload = {
            "task_id": task["id"],
            "title": scope["title"],
            "brief": BRIEF,
            "rules": scope["rules"],
            "operations": scope["operations"],
            "callers": scope["callers"],
            "limits": LIMITS,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        (OUTPUT / f"{task['id']}.json").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(manifest['tasks'])} task files to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
