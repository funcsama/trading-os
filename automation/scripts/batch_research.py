"""A 股批量深度研究调度器。

读取 coverage/cn-a/research_queue.jsonl 中 status=pending 的条目，为每家公司启动一个
非交互 `claude -p` 子进程做深度研究。每个 worker 收到一份自包含
单公司研究提示词（见 _worker_prompt.md），不需要阅读仓库内其他说明文件，只写各自公司目录。
主调度器负责状态更新、公司目录校验、全局索引重建和最终提交。

核心稳健原则：
- 低并发（默认 2）以避免 429 rate limit。
- 以磁盘 artifact（meta.json + 报告）存在性为准，而非 claude 退出码。
- 429 等 transient 失败会记录为 failed，后续可用 --include-failed 重试。
- 支持 --include-failed 在重跑时清理并重试失败项。

用法:
    python automation/scripts/batch_research.py --concurrency 2 --max 30
    python automation/scripts/batch_research.py --tickers CN:600519,CN:300750
    python automation/scripts/batch_research.py --concurrency 2 --dry-run
    python automation/scripts/batch_research.py --probe-only

约束:
    - 只调度 status=pending（或 --tickers 指定）的 initial_research。
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
CLAUDE_ENV_KEYS_TO_CLEAN = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN2",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_REASONING_MODEL",
    "CLAUDE_MODEL_OPUS",
    "CLAUDE_MODEL",
}

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
               task_type: str = "initial_research",
               started_at_override: str | None = None) -> None:
    with _FILES_LOCK:
        existing = next(
            (item for item in read_jsonl(COVERAGE_ROOT / RESEARCH_QUEUE_FILE)
             if item.get("symbol") == symbol),
            {},
        )
        started_at = started_at_override or existing.get("started_at")
        if status == "running" and not started_at:
            started_at = _now_iso()
        next_action = existing.get("next_action")
        if status == "completed":
            next_action = "见 reports/ 下最新报告。"
        elif status == "failed":
            next_action = "修复失败原因后重试；必要时改用主 agent 联网收集资料。"
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
            "next_action": next_action,
        }
        upsert_jsonl(COVERAGE_ROOT / RESEARCH_QUEUE_FILE, "symbol", record)


def append_run(record: dict[str, Any]) -> None:
    path = COVERAGE_ROOT / RUNS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILES_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def claude_env(*, clean: bool) -> dict[str, str]:
    env = dict(os.environ)
    settings_env = _load_claude_settings_env()
    if clean:
        for key in CLAUDE_ENV_KEYS_TO_CLEAN:
            env.pop(key, None)
        env.update(settings_env)
    else:
        for key, value in settings_env.items():
            env.setdefault(key, value)
    return env


def _first_env_value(env: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = env.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _load_claude_settings_env() -> dict[str, str]:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    env = payload.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if value is not None}


def detect_claude_tool_label() -> str:
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "--version"],
            cwd=str(WORKER_CWD),
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:  # noqa: BLE001
        return "Claude Code"
    version = (proc.stdout or proc.stderr or "").strip()
    if not version:
        return "Claude Code"
    return f"Claude Code {version}"


def detect_claude_model_label(*, clean_env: bool, explicit_model: str | None) -> str:
    if explicit_model and explicit_model.strip():
        return explicit_model.strip()
    effective_env = claude_env(clean=clean_env)
    model = _first_env_value(
        effective_env,
        [
            "CLAUDE_MODEL",
            "CLAUDE_MODEL_OPUS",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
        ],
    )
    if model:
        return model
    settings_env = _load_claude_settings_env()
    model = _first_env_value(
        settings_env,
        [
            "CLAUDE_MODEL",
            "CLAUDE_MODEL_OPUS",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
        ],
    )
    return model or "model unknown"


def build_analyst_id(
    *,
    clean_env: bool,
    explicit_analyst_id: str | None,
    explicit_model: str | None,
) -> str:
    if explicit_analyst_id and explicit_analyst_id.strip():
        return explicit_analyst_id.strip()
    return (
        f"{detect_claude_tool_label()} + "
        f"{detect_claude_model_label(clean_env=clean_env, explicit_model=explicit_model)}"
    )


def claude_base_cmd(*, safe_mode: bool) -> list[str]:
    cmd = [
        CLAUDE_BIN,
        "--add-dir",
        str(ROOT),
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--output-format",
        "text",
    ]
    if safe_mode:
        cmd.append("--safe-mode")
    return cmd


def probe_claude(*, clean_env: bool, safe_mode: bool, timeout_s: int) -> tuple[bool, str]:
    probe_path = ROOT / "automation" / "scripts" / "_claude_probe.tmp"
    if probe_path.exists():
        probe_path.unlink()
    prompt = (
        "在 automation/scripts 下写一个名为 _claude_probe.tmp 的文件，内容为 OK。"
        "完成后只输出一行：__RESULT__{\"ok\":true}"
    )
    cmd = [CLAUDE_BIN, "-p", prompt, *claude_base_cmd(safe_mode=safe_mode)[1:]]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORKER_CWD),
            env=claude_env(clean=clean_env),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"probe timeout after {timeout_s}s"
    file_ok = probe_path.exists() and probe_path.read_text(encoding="utf-8").strip() == "OK"
    if probe_path.exists():
        probe_path.unlink()
    output = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result_ok = "__RESULT__" in output and '"ok":true' in output.replace(" ", "")
    if proc.returncode == 0 and file_ok and result_ok:
        return True, "probe ok"
    detail = err or output or f"rc={proc.returncode}, file_ok={file_ok}, result_ok={result_ok}"
    return False, detail[:500]


def validate_company_dir(target_company_dir: str) -> tuple[bool, str]:
    vcmd = [
        sys.executable,
        "-m",
        "trading_os",
        "company",
        "validate",
        str(WORKER_CWD / target_company_dir),
    ]
    try:
        vproc = subprocess.run(
            vcmd,
            cwd=str(WORKER_CWD),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"validate exception: {exc}"
    if vproc.returncode != 0:
        return False, (vproc.stderr or vproc.stdout).strip()[:300]
    return True, ""


def salvage_company_result(target_company_dir: str) -> dict[str, Any] | None:
    company_dir = WORKER_CWD / target_company_dir
    meta_path = company_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None
    result_path = meta.get("latest_report")
    if not isinstance(result_path, str) or result_path.endswith("-failed.md"):
        return None
    if not (company_dir / result_path).exists():
        return None
    valid, detail = validate_company_dir(target_company_dir)
    if not valid:
        return None
    return {
        "report_path": result_path,
        "rating": meta.get("current_rating"),
        "buy_zone": meta.get("buy_zone"),
        "fair_value_range": meta.get("fair_value_range"),
        "note": detail,
    }


def build_prompt(
    symbol: str,
    name: str,
    *,
    reason: str,
    priority: int,
    target_company_dir: str,
    company_snapshot: dict[str, Any] | None,
    analyst_id: str,
    market: str = "CN",
) -> str:
    ticker = _ticker(symbol)
    tmpl = PROMPT_FILE.read_text(encoding="utf-8")
    return (
        tmpl
        .replace("{{COMPANY_NAME}}", name)
        .replace("{{SYMBOL}}", symbol)
        .replace("{{MARKET}}", market)
        .replace("{{TICKER}}", ticker)
        .replace("{{COMPANY_DIR}}", target_company_dir)
        .replace("{{DATE}}", _dt.date.today().isoformat())
        .replace("{{SLUG}}", "initial")
        .replace("{{RESEARCH_REASON}}", reason)
        .replace("{{PRIORITY}}", str(priority))
        .replace("{{ANALYST_ID}}", analyst_id)
        .replace(
            "{{COMPANY_SNAPSHOT_JSON}}",
            json.dumps(company_snapshot or {}, ensure_ascii=False, indent=2, sort_keys=True),
        )
    )


def run_claude_worker(symbol: str, name: str, reason: str, priority: int,
                      target_company_dir: str, timeout_s: int,
                      company_snapshot: dict[str, Any] | None,
                      *, clean_env: bool, safe_mode: bool,
                      analyst_id: str, claude_model: str | None) -> dict[str, Any]:
    """启动一个 claude -p 子进程，写一个公司的研究产物。返回 run 记录。"""
    started = _now_iso()
    mark_queue(symbol, name, reason, priority, "running",
               target_company_dir=target_company_dir,
               started_at_override=started)
    prompt = build_prompt(
        symbol,
        name,
        reason=reason,
        priority=priority,
        target_company_dir=target_company_dir,
        company_snapshot=company_snapshot,
        analyst_id=analyst_id,
    )
    # 关键 flags: 非交互、跳过权限确认（worker 在仓库内只写自己目录，已在 prompt 中强约束）、
    # 限定目录、流式 JSON 之外用纯文本输出便于正则解析。
    cmd = [CLAUDE_BIN, "-p", prompt, *claude_base_cmd(safe_mode=safe_mode)[1:]]
    if claude_model:
        cmd.extend(["--model", claude_model])
    env = claude_env(clean=clean_env)
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
        valid, detail = validate_company_dir(target_company_dir)
        if not valid:
            ok = False
            errors.append(f"validate failed: {detail}")
    if not ok:
        salvaged = salvage_company_result(target_company_dir)
        if salvaged is not None:
            ok = True
            result_path = salvaged.get("report_path")
            rating = salvaged.get("rating")
            buy_zone = salvaged.get("buy_zone")
            fair_value = salvaged.get("fair_value_range")
            errors.append("salvaged valid company asset after missing RESULT or timeout")
    status = "completed" if ok else "failed"
    failure_reason = None
    if not ok:
        failure_reason = (err.strip()[:500] if err else "") or " ".join(errors)[:500] or f"rc={rc}"
    mark_queue(symbol, name, reason, priority, status,
               result_path=result_path, failure_reason=failure_reason,
               target_company_dir=target_company_dir,
               started_at_override=started)
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
    ap.add_argument("--concurrency", type=int, default=2,
                    help="并发数（默认2，避免429限流）")
    ap.add_argument("--max", type=int, default=None, help="本轮最多处理多少家")
    ap.add_argument("--tickers", help="只处理指定 symbol，逗号分隔，覆盖 pending 选择")
    ap.add_argument("--timeout", type=int, default=3600, help="单家 claude 超时秒数（默认3600=1h）")
    ap.add_argument("--include-failed", action="store_true",
                    help="同时重试 status=failed 的队列项")
    ap.add_argument("--claude-safe-mode", action="store_true",
                    help="用 Claude safe-mode 运行 worker，排除插件、状态栏和项目自定义干扰")
    ap.add_argument(
        "--claude-model",
        help="显式指定 Claude worker 使用的模型，并传给 claude --model",
    )
    ap.add_argument(
        "--analyst-id",
        help="显式写入报告的分析师标识，如 'Claude Code 2.1.195 + glm-5.2'",
    )
    ap.add_argument(
        "--no-clean-claude-env",
        action="store_true",
        help="默认清理外层 ANTHROPIC_* 覆盖变量，并注入 ~/.claude/settings.json 的 env；该选项关闭清理",
    )
    ap.add_argument("--skip-claude-probe", action="store_true",
                    help="跳过 Claude 文件写入探针；只建议在已知 worker 可用时使用")
    ap.add_argument("--probe-only", action="store_true",
                    help="只运行 Claude 文件写入探针，不处理队列")
    ap.add_argument("--probe-timeout", type=int, default=180,
                    help="Claude 文件写入探针超时秒数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    clean_env = not args.no_clean_claude_env
    analyst_id = build_analyst_id(
        clean_env=clean_env,
        explicit_analyst_id=args.analyst_id,
        explicit_model=args.claude_model,
    )

    if args.probe_only:
        ok, detail = probe_claude(
            clean_env=clean_env,
            safe_mode=args.claude_safe_mode,
            timeout_s=args.probe_timeout,
        )
        print(f"[batch] claude probe: {'ok' if ok else 'failed'} - {detail}")
        return 0 if ok else 2

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
    print(f"[batch] analyst_id={analyst_id}")

    if args.dry_run:
        for t in targets[:50]:
            print("  -", t["symbol"], t.get("name"))
        return 0

    if not args.skip_claude_probe:
        ok, detail = probe_claude(
            clean_env=clean_env,
            safe_mode=args.claude_safe_mode,
            timeout_s=args.probe_timeout,
        )
        if not ok:
            print(f"[batch] Claude probe failed: {detail}", file=sys.stderr)
            print(
                "[batch] 未派发任何研究任务。可修复 Claude 后重试，"
                "或显式传 --skip-claude-probe。"
            )
            return 2

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
                              args.timeout, companies.get(item["symbol"]),
                              clean_env=clean_env,
                              safe_mode=args.claude_safe_mode,
                              analyst_id=analyst_id,
                              claude_model=args.claude_model)
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
                detail = rec.get("result_path") or rec.get("failure_reason", "")[:80]
                print(
                    f"[batch] {std:9s} {item['symbol']} {item.get('name',''):<14s} "
                    f"rc={rec.get('returncode')} -> {detail}",
                    flush=True,
                )
                print(f"[batch] 进度 {done}/{len(targets)}  ok={ok_count}  "
                      f"耗时 {time.time()-st:.0f}s", flush=True)
                # 派发下一个
                if not _STOP.is_set():
                    item2 = next(it, None)
                    if item2 is not None:
                        fut2 = pool.submit(
                            run_claude_worker,
                            item2["symbol"],
                            item2.get("name", ""),
                            item2.get("reason", ""),
                            int(item2.get("priority", 9)),
                            item2.get("target_company_dir")
                            or f"research/companies/CN/{_ticker(item2['symbol'])}",
                            args.timeout,
                            companies.get(item2["symbol"]),
                            clean_env=clean_env,
                            safe_mode=args.claude_safe_mode,
                            analyst_id=analyst_id,
                            claude_model=args.claude_model,
                        )
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
