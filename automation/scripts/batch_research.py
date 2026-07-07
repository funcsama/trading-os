"""A 股批量深度研究调度器。

读取 coverage/cn-a/research_queue.jsonl 中 status=pending 的条目，为每家公司启动一个
非交互 `claude -p` 子进程做深度研究，全局并发上限默认 10。每个 worker 收到一份自包含
单公司研究提示词（见 _worker_prompt.md），不需要阅读仓库内其他说明文件，只写各自公司目录。
主调度器负责状态更新、公司目录校验、全局索引重建和最终提交。

用法:
    python automation/scripts/batch_research.py --concurrency 10 --max 30
    python automation/scripts/batch_research.py --tickers CN:600519,CN:300750
    python automation/scripts/batch_research.py --concurrency 10 --dry-run

约束:
    - 只调度 status=pending（或 --tickers 指定）的 initial_research。
    - 跳过已存在但 status != pending 的项（避免重复）。
    - 进程中断会通过 runs.jsonl 与队列 status 可恢复（下次运行复用 pending/failed 项）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading_os.research_assets.coverage_store import (  # noqa: E402
    COMPANIES_FILE,
    RESEARCH_QUEUE_FILE,
    RUNS_FILE,
    read_jsonl,
    upsert_jsonl,
)

COVERAGE_ROOT = ROOT / "coverage" / "cn-a"
COMPANIES_ROOT = ROOT / "research" / "companies"
PROMPT_FILE = ROOT / "automation" / "scripts" / "_worker_prompt.md"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# 让 worker 自己的 cwd 始终是仓库根，避免相对路径漂移。
WORKER_CWD = ROOT

RESULT_RE = re.compile(r"__RESULT__(\{.*\})")

_STOP = threading.Event()
_FILES_LOCK = threading.Lock()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat()


def _slug(name: str) -> str:
    # 中文名没法直接做 slug，initial/followup 已足够；保留首尾安全字符。
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "x"


def _ticker(symbol: str) -> str:
    return symbol.split(":", 1)[1]


def load_queue() -> list[dict[str, Any]]:
    return read_jsonl(COVERAGE_ROOT / RESEARCH_QUEUE_FILE)


def load_companies() -> dict[str, dict[str, Any]]:
    return {
        item["symbol"]: item
        for item in read_jsonl(COVERAGE_ROOT / COMPANIES_FILE)
        if isinstance(item.get("symbol"), str)
    }


def mark_queue(symbol: str, name: str, reason: str, priority: int,
               status: str, *, result_path: str | None = None,
               failure_reason: str | None = None,
               target_company_dir: str | None = None,
               task_type: str = "initial_research") -> None:
    with _FILES_LOCK:
        existing = next(
            (item for item in read_jsonl(COVERAGE_ROOT / RESEARCH_QUEUE_FILE)
             if item.get("symbol") == symbol),
            {},
        )
        started_at = existing.get("started_at")
        if status == "running" and not started_at:
            started_at = _now_iso()
        record = {
            **existing,
            "symbol": symbol,
            "name": name,
            "task_type": task_type,
            "priority": priority,
            "status": status,
            "reason": reason,
            "target_company_dir": target_company_dir or f"research/companies/CN/{_ticker(symbol)}",
            "assigned_agent": "batch_research.py",
            "started_at": started_at,
            "finished_at": _now_iso() if status in {"completed", "failed", "skipped"} else None,
            "result_path": result_path,
            "failure_reason": failure_reason,
            "next_action": "见 reports/ 下最新报告。" if status == "completed" else existing.get("next_action"),
        }
        upsert_jsonl(COVERAGE_ROOT / RESEARCH_QUEUE_FILE, "symbol", record)


def append_run(record: dict[str, Any]) -> None:
    path = COVERAGE_ROOT / RUNS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILES_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_prompt(
    symbol: str,
    name: str,
    *,
    reason: str,
    priority: int,
    target_company_dir: str,
    company_snapshot: dict[str, Any] | None,
    market: str = "CN",
) -> str:
    ticker = _ticker(symbol)
    tmpl = PROMPT_FILE.read_text(encoding="utf-8")
    return (tmpl
            .replace("{{COMPANY_NAME}}", name)
            .replace("{{SYMBOL}}", symbol)
            .replace("{{MARKET}}", market)
            .replace("{{TICKER}}", ticker)
            .replace("{{COMPANY_DIR}}", target_company_dir)
            .replace("{{DATE}}", _dt.date.today().isoformat())
            .replace("{{SLUG}}", "initial")
            .replace("{{RESEARCH_REASON}}", reason)
            .replace("{{PRIORITY}}", str(priority))
            .replace(
                "{{COMPANY_SNAPSHOT_JSON}}",
                json.dumps(company_snapshot or {}, ensure_ascii=False, indent=2, sort_keys=True),
            ))


def run_claude_worker(symbol: str, name: str, reason: str, priority: int,
                      target_company_dir: str, timeout_s: int,
                      company_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """启动一个 claude -p 子进程，写一个公司的研究产物。返回 run 记录。"""
    started = _now_iso()
    mark_queue(symbol, name, reason, priority, "running",
               target_company_dir=target_company_dir)
    prompt = build_prompt(
        symbol,
        name,
        reason=reason,
        priority=priority,
        target_company_dir=target_company_dir,
        company_snapshot=company_snapshot,
    )
    # 关键 flags: 非交互、跳过权限确认（worker 在仓库内只写自己目录，已在 prompt 中强约束）、
    # 限定目录、流式 JSON 之外用纯文本输出便于正则解析。
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--add-dir", str(ROOT),
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]
    env = dict(os.environ)
    proc_started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(WORKER_CWD), env=env,
            capture_output=True, text=True, timeout=timeout_s,
            encoding="utf-8", errors="replace",
        )
        out, err = proc.stdout, proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        out, err = "", "timeout after %ss" % timeout_s
    finished = _now_iso()
    # 解析最后一行 __RESULT__
    ok = rc == 0
    result_path = None
    rating = None
    buy_zone: list[float] | None = None
    fair_value: list[float] | None = None
    errors: list[str] = []
    if ok:
        m = RESULT_RE.search(out or "")
        if m:
            try:
                rj = json.loads(m.group(1))
                ok = bool(rj.get("ok", False))
                result_path = rj.get("report_path")
                rating = rj.get("rating")
                buy_zone = rj.get("buy_zone")
                fair_value = rj.get("fair_value_range")
                errors = list(rj.get("errors") or [])
            except json.JSONDecodeError as e:
                errors.append(f"result json parse: {e}")
        if ok and not result_path:
            # 兜底：在公司目录 reports/ 找今日最新 .md
            rdir = WORKER_CWD / target_company_dir / "reports"
            if rdir.exists():
                mds = sorted(rdir.glob("*.md"))
                if mds:
                    result_path = f"reports/{mds[-1].name}"
        if ok:
            vcmd = [
                sys.executable,
                "-m",
                "trading_os",
                "company",
                "validate",
                str(WORKER_CWD / target_company_dir),
            ]
            try:
                vproc = subprocess.run(vcmd, cwd=str(WORKER_CWD), capture_output=True,
                                        text=True, timeout=120)
                if vproc.returncode != 0:
                    ok = False
                    errors.append(f"validate failed: {(vproc.stderr or vproc.stdout).strip()[:300]}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"validate exception: {e}")
                ok = False
    status = "completed" if ok else "failed"
    failure_reason = None
    if not ok:
        failure_reason = (err.strip()[:500] if err else "") or " ".join(errors)[:500] or f"rc={rc}"
    mark_queue(symbol, name, reason, priority, status,
               result_path=result_path, failure_reason=failure_reason,
               target_company_dir=target_company_dir)
    run_record = {
        "run_id": f"batch-{symbol}-{int(proc_started)}",
        "as_of": _dt.date.today().isoformat(),
        "run_type": "initial_research",
        "symbol": symbol, "name": name,
        "started_at": started, "finished_at": finished,
        "returncode": rc,
        "status": status,
        "result_path": result_path,
        "rating": rating,
        "buy_zone": buy_zone,
        "fair_value_range": fair_value,
        "failure_reason": failure_reason,
        "errors": errors[:5],
        "stdout_tail": (out or "").strip()[-400:],
        "stderr_tail": (err or "").strip()[-400:],
    }
    append_run(run_record)
    return run_record


def select_targets(queue: list[dict[str, Any]], tickers: list[str] | None,
                  max_n: int | None, include_failed: bool) -> list[dict[str, Any]]:
    statuses = {"pending", "failed"} if include_failed else {"pending"}
    pending = [r for r in queue if r.get("status") in statuses
               and r.get("task_type", "initial_research") == "initial_research"]
    if tickers:
        want = set(tickers)
        pending = [r for r in pending if r["symbol"] in want]
    # priority 升序（1 最高）
    pending.sort(key=lambda r: (int(r.get("priority", 9)), r["symbol"]))
    if max_n is not None:
        pending = pending[:max_n]
    return pending


def main() -> int:
    ap = argparse.ArgumentParser(description="A 股批量深度研究调度器")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--max", type=int, default=None, help="本轮最多处理多少家")
    ap.add_argument("--tickers", help="只处理指定 symbol，逗号分隔，覆盖 pending 选择")
    ap.add_argument("--timeout", type=int, default=1800, help="单家 claude 超时秒数")
    ap.add_argument("--include-failed", action="store_true",
                    help="同时重试 status=failed 的队列项")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None

    def _handle_signal(signum, _frame):
        print(f"[batch] 收到信号 {signum}，停止派发新任务，等待在途完成…", flush=True)
        _STOP.set()
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass

    queue = load_queue()
    companies = load_companies()
    targets = select_targets(queue, tickers, args.max, args.include_failed)
    print(f"[batch] 待研究 {len(targets)} 家 / 并发 {args.concurrency} / 超时 {args.timeout}s"
          f" / dry_run={args.dry_run}")

    if args.dry_run:
        for t in targets[:50]:
            print("  -", t["symbol"], t.get("name"))
        return 0

    done = 0
    ok_count = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: dict[Future, tuple[dict[str, Any], float]] = {}
        it = iter(targets)
        # 初始化派发
        for _ in range(args.concurrency):
            item = next(it, None)
            if item is None or _STOP.is_set():
                break
            fut = pool.submit(run_claude_worker, item["symbol"], item.get("name", ""),
                              item.get("reason", ""), int(item.get("priority", 9)),
                              item.get("target_company_dir")
                              or f"research/companies/CN/{_ticker(item['symbol'])}",
                              args.timeout, companies.get(item["symbol"]))
            futures[fut] = (item, time.time())

        while futures:
            if _STOP.is_set():
                # 不再派发；等剩余完成
                done_set, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
            else:
                done_set, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED, timeout=5)
                if not done_set:
                    continue
            for fut in done_set:
                item, st = futures.pop(fut)
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    rec = {"symbol": item["symbol"], "status": "failed",
                           "failure_reason": f"worker exception: {e}"}
                    append_run({"run_id": f"batch-{item['symbol']}-exc",
                                "as_of": _dt.date.today().isoformat(),
                                "symbol": item["symbol"], "name": item.get("name"),
                                "status": "failed", "failure_reason": str(e)[:500]})
                done += 1
                std = rec.get("status")
                if std == "completed":
                    ok_count += 1
                print(f"[batch] {std:9s} {item['symbol']} {item.get('name',''):<14s} "
                      f"rc={rec.get('returncode')} -> {rec.get('result_path') or rec.get('failure_reason','')[:80]}",
                      flush=True)
                print(f"[batch] 进度 {done}/{len(targets)}  ok={ok_count}  "
                      f"耗时 {time.time()-st:.0f}s", flush=True)
                # 派发下一个
                if not _STOP.is_set():
                    item2 = next(it, None)
                    if item2 is not None:
                        fut2 = pool.submit(run_claude_worker, item2["symbol"], item2.get("name", ""),
                                           item2.get("reason", ""), int(item2.get("priority", 9)),
                                           item2.get("target_company_dir")
                                           or f"research/companies/CN/{_ticker(item2['symbol'])}",
                                           args.timeout, companies.get(item2["symbol"]))
                        futures[fut2] = (item2, time.time())
            # 定期重建索引（每 10 家一次）
            if done % 10 == 0 and done > 0:
                try:
                    subprocess.run([sys.executable, "-m", "trading_os", "index", "rebuild"],
                                   cwd=str(ROOT), capture_output=True, timeout=60)
                except Exception:  # noqa: BLE001
                    pass

    print(f"[batch] 完成。total={done} ok={ok_count} fail={done-ok_count}", flush=True)
    # 收尾：重建生成件
    for sub in (["index", "rebuild"], ["schedule", "build"], ["alerts", "build"]):
        try:
            subprocess.run([sys.executable, "-m", "trading_os", *sub], cwd=str(ROOT),
                           capture_output=True, timeout=120)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
