"""Render a self-contained single-company research prompt for a Codex subagent.

This helper does not dispatch work. It reads the existing coverage queue and
company snapshot, fills `automation/scripts/_worker_prompt.md`, and prints the
result to stdout or writes it to a file. The main agent can then pass that prompt
directly to a normal Codex subagent.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading_os.research_assets.coverage_store import read_jsonl  # noqa: E402

COVERAGE_ROOT = ROOT / "coverage" / "cn-a"
PROMPT_FILE = ROOT / "automation" / "scripts" / "_worker_prompt.md"


def ticker_from_symbol(symbol: str) -> str:
    return symbol.split(":", 1)[1]


def load_by_symbol(path: Path) -> dict[str, dict[str, Any]]:
    return {
        item["symbol"]: item
        for item in read_jsonl(path)
        if isinstance(item.get("symbol"), str)
    }


def render_prompt(symbol: str, *, analyst_id: str | None = None, date: str | None = None) -> str:
    queue = load_by_symbol(COVERAGE_ROOT / "research_queue.jsonl")
    companies = load_by_symbol(COVERAGE_ROOT / "companies.jsonl")
    if symbol not in queue:
        raise SystemExit(f"symbol not found in research_queue.jsonl: {symbol}")
    if symbol not in companies:
        raise SystemExit(f"symbol not found in companies.jsonl: {symbol}")

    item = queue[symbol]
    company = companies[symbol]
    market = symbol.split(":", 1)[0]
    ticker = ticker_from_symbol(symbol)
    today = date or dt.date.today().isoformat()
    company_dir = item.get("target_company_dir") or f"research/companies/{market}/{ticker}"
    analyst = analyst_id or f"codex-subagent-{ticker}"

    replacements = {
        "{{COMPANY_NAME}}": item.get("name") or company.get("name") or ticker,
        "{{SYMBOL}}": symbol,
        "{{MARKET}}": market,
        "{{TICKER}}": ticker,
        "{{DATE}}": today,
        "{{ANALYST_ID}}": analyst,
        "{{PRIORITY}}": str(item.get("priority") or 5),
        "{{RESEARCH_REASON}}": item.get("reason") or "进入研究队列。",
        "{{COMPANY_DIR}}": company_dir,
        "{{SLUG}}": "initial",
        "{{COMPANY_SNAPSHOT_JSON}}": json.dumps(
            company,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    }
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        prompt = prompt.replace(key, str(value))
    return prompt


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a pure worker prompt for one company.")
    parser.add_argument("symbol", help="Company symbol, e.g. CN:601398")
    parser.add_argument("--analyst-id")
    parser.add_argument("--date")
    parser.add_argument("--out", type=Path, help="Write prompt to this file instead of stdout.")
    args = parser.parse_args()

    prompt = render_prompt(args.symbol, analyst_id=args.analyst_id, date=args.date)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(prompt, encoding="utf-8")
    else:
        sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
