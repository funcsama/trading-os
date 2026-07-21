"""Resumable, vendor-neutral dispatcher for independent underwriting agents."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from automation.scripts.build_review_prompt import (  # noqa: E402
    PromptBuildError,
    build_run_prompt,
)
from trading_os.research_assets.models import ReviewRunStatus, load_policy  # noqa: E402
from trading_os.research_assets.portfolio import (  # noqa: E402
    POLICY_KEYS,
    build_model_portfolio,
)
from trading_os.research_assets.review_store import (  # noqa: E402
    ReviewRunStore,
    ReviewStoreError,
)
from trading_os.research_assets.sealing import seal_json, verify_sealed  # noqa: E402


class DispatchError(ValueError):
    """Raised when a review stage cannot be safely dispatched."""


@dataclass(frozen=True, slots=True)
class AgentTask:
    run_id: str
    task_id: str
    stage: str
    symbol: str
    prompt: str
    output_path: Path
    allowed_read_paths: tuple[Path, ...]
    allowed_write_paths: tuple[Path, ...]
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class AgentResult:
    ok: bool
    payload: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    run_id: str
    stage: str
    status: str
    completed: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


class Runner(Protocol):
    def run(self, task: AgentTask) -> AgentResult: ...


class SubprocessRunner:
    """Execute any JSON-producing agent command with the prompt on stdin."""

    def __init__(self, command: Sequence[str], *, cwd: str | Path = ROOT):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise DispatchError("runner command must be a non-empty string sequence")
        self.command = tuple(command)
        self.cwd = Path(cwd)

    def run(self, task: AgentTask) -> AgentResult:
        try:
            process = subprocess.run(
                self.command,
                input=task.prompt,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=task.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, error=f"timeout after {task.timeout_seconds}s")
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "runner failed").strip()
            return AgentResult(ok=False, error=detail[-1000:])
        try:
            payload = _parse_runner_payload(process.stdout)
        except DispatchError as exc:
            return AgentResult(ok=False, error=str(exc))
        return AgentResult(ok=True, payload=payload)


class ReviewDispatcher:
    def __init__(
        self,
        *,
        runs_root: str | Path,
        policy_root: str | Path,
        runner: Runner,
        owner: str = "review-dispatch",
        concurrency: int = 4,
        timeout_seconds: int = 3600,
        lease_seconds: int = 3900,
    ):
        if concurrency <= 0:
            raise DispatchError("concurrency must be positive")
        if timeout_seconds <= 0 or lease_seconds <= 0:
            raise DispatchError("timeouts must be positive")
        if lease_seconds <= timeout_seconds:
            raise DispatchError("lease_seconds must exceed timeout_seconds")
        self.runs_root = Path(runs_root)
        self.policy_root = Path(policy_root)
        self.runner = runner
        self.owner = owner
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.lease_seconds = lease_seconds
        self.store = ReviewRunStore(self.runs_root)

    def dispatch(self, run_id: str, *, now: dt.datetime) -> DispatchResult:
        _require_aware(now)
        status = self.store.load_run(run_id)["status"]
        if status in {
            ReviewRunStatus.PACKETS_READY.value,
            ReviewRunStatus.BLIND_REVIEWING.value,
        }:
            return self._dispatch_blind(run_id, now=now)
        if status in {
            ReviewRunStatus.BLIND_SEALED.value,
            ReviewRunStatus.REVEALING.value,
        }:
            return self._dispatch_reveal(run_id, now=now)
        if status == ReviewRunStatus.CHALLENGING.value:
            return self._dispatch_challenge_and_arbitration(run_id, now=now)
        if status in {
            ReviewRunStatus.COMPANY_REVIEWS_COMPLETE.value,
            ReviewRunStatus.SYNTHESIZING.value,
            ReviewRunStatus.COMPLETED.value,
        }:
            return DispatchResult(run_id, "none", status, (), ())
        raise DispatchError(f"review run is not dispatchable from status: {status}")

    def _dispatch_blind(self, run_id: str, *, now: dt.datetime) -> DispatchResult:
        state = self.store.load_run(run_id)
        if state["status"] == ReviewRunStatus.PACKETS_READY.value:
            self.store.transition(
                run_id,
                ReviewRunStatus.BLIND_REVIEWING.value,
                actor=self.owner,
                at=now,
            )
        candidates = self.store.read_candidates(run_id)
        completed, failed = self._parallel(
            candidates,
            lambda item: self._run_stage(run_id, item, "blind", now=now),
        )
        if not failed:
            state = self.store.transition(
                run_id,
                ReviewRunStatus.BLIND_SEALED.value,
                actor=self.owner,
                at=now,
            )
        else:
            state = self.store.load_run(run_id)
        return DispatchResult(
            run_id,
            "blind",
            state["status"],
            tuple(sorted(completed)),
            tuple(sorted(failed)),
        )

    def _dispatch_reveal(self, run_id: str, *, now: dt.datetime) -> DispatchResult:
        state = self.store.load_run(run_id)
        if state["status"] == ReviewRunStatus.BLIND_SEALED.value:
            self.store.transition(
                run_id,
                ReviewRunStatus.REVEALING.value,
                actor=self.owner,
                at=now,
            )
        candidates = self.store.read_candidates(run_id)
        completed, failed = self._parallel(
            candidates,
            lambda item: self._run_stage(run_id, item, "reveal", now=now),
        )
        if not failed:
            needs_challenger = [
                item
                for item in candidates
                if _read_json_object(_artifact_path(item, run_id, "reveal"))[
                    "challenger_required"
                ]
            ]
            for item in candidates:
                if item not in needs_challenger:
                    reveal = _read_json_object(_artifact_path(item, run_id, "reveal"))
                    self._seal_portfolio_candidate(
                        item,
                        run_id,
                        reveal["portfolio_candidate"],
                        now=now,
                    )
            next_status = (
                ReviewRunStatus.CHALLENGING.value
                if needs_challenger
                else ReviewRunStatus.COMPANY_REVIEWS_COMPLETE.value
            )
            state = self.store.transition(
                run_id,
                next_status,
                actor=self.owner,
                at=now,
            )
        else:
            state = self.store.load_run(run_id)
        return DispatchResult(
            run_id,
            "reveal",
            state["status"],
            tuple(sorted(completed)),
            tuple(sorted(failed)),
        )

    def _dispatch_challenge_and_arbitration(
        self, run_id: str, *, now: dt.datetime
    ) -> DispatchResult:
        candidates = [
            item
            for item in self.store.read_candidates(run_id)
            if _read_json_object(_artifact_path(item, run_id, "reveal"))[
                "challenger_required"
            ]
        ]

        def work(item: Mapping[str, Any]) -> str:
            self._run_stage(run_id, item, "challenger", now=now)
            self._run_stage(run_id, item, "arbitration", now=now)
            arbitration = _read_json_object(_artifact_path(item, run_id, "arbitration"))
            self._seal_portfolio_candidate(
                item,
                run_id,
                arbitration["portfolio_candidate"],
                now=now,
            )
            return str(item["symbol"])

        completed, failed = self._parallel(candidates, work)
        if not failed:
            state = self.store.transition(
                run_id,
                ReviewRunStatus.COMPANY_REVIEWS_COMPLETE.value,
                actor=self.owner,
                at=now,
            )
        else:
            state = self.store.load_run(run_id)
        return DispatchResult(
            run_id,
            "challenge_and_arbitration",
            state["status"],
            tuple(sorted(completed)),
            tuple(sorted(failed)),
        )

    def _run_stage(
        self,
        run_id: str,
        candidate: Mapping[str, Any],
        stage: str,
        *,
        now: dt.datetime,
    ) -> str:
        symbol = str(candidate["symbol"])
        task_id = f"{symbol.replace(':', '-')}-{stage}"
        output_path = _artifact_path(candidate, run_id, stage)
        lease = self.store.acquire_lease(
            run_id,
            task_id,
            owner=self.owner,
            now=now,
            ttl_seconds=self.lease_seconds,
        )
        if lease.status == "completed":
            verify_sealed(output_path)
            return symbol
        if output_path.exists():
            verify_sealed(output_path)
            self.store.complete_lease(
                run_id,
                task_id,
                owner=self.owner,
                completed_at=now,
                result_path=output_path.as_posix(),
            )
            return symbol
        try:
            prompt = build_run_prompt(
                stage=stage,
                run_id=run_id,
                symbol=symbol,
                runs_root=self.runs_root,
                policy_root=self.policy_root,
            )
            task = AgentTask(
                run_id=run_id,
                task_id=task_id,
                stage=stage,
                symbol=symbol,
                prompt=prompt,
                output_path=output_path,
                allowed_read_paths=_allowed_reads(candidate, run_id, stage),
                allowed_write_paths=(output_path,),
                timeout_seconds=self.timeout_seconds,
            )
            result = self.runner.run(task)
            if not isinstance(result, AgentResult):
                raise DispatchError("runner must return AgentResult")
            if not result.ok:
                raise DispatchError(result.error or "agent failed without an error")
            if not isinstance(result.payload, Mapping):
                raise DispatchError("successful agent result must contain an object payload")
            payload = dict(result.payload)
            _validate_stage_payload(stage, payload, symbol, self._portfolio_policy())
            seal_json(
                output_path,
                payload,
                artifact_type=f"{stage}_assessment",
                sealed_at=now,
            )
            self.store.complete_lease(
                run_id,
                task_id,
                owner=self.owner,
                completed_at=now,
                result_path=output_path.as_posix(),
            )
            return symbol
        except Exception:
            self.store.release_lease(run_id, task_id, owner=self.owner)
            raise

    def _seal_portfolio_candidate(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        payload: Any,
        *,
        now: dt.datetime,
    ) -> None:
        if not isinstance(payload, Mapping):
            raise DispatchError("portfolio_candidate must be an object")
        normalized = dict(payload)
        _validate_portfolio_candidate(
            normalized,
            str(candidate["symbol"]),
            self._portfolio_policy(),
        )
        seal_json(
            Path(candidate["target_company_dir"])
            / "underwriting"
            / run_id
            / "portfolio-candidate.json",
            normalized,
            artifact_type="portfolio_candidate",
            sealed_at=now,
        )

    def _portfolio_policy(self) -> dict[str, Any]:
        policy = load_policy(self.policy_root / "portfolio.json")
        return {key: policy.payload[key] for key in POLICY_KEYS}

    def _parallel(self, candidates, worker):
        completed: list[str] = []
        failed: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(worker, item): item for item in candidates}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    completed.append(future.result())
                except Exception as exc:  # failure isolation is deliberate
                    failed.append((str(item["symbol"]), str(exc)))
        return completed, failed


def _validate_stage_payload(
    stage: str,
    payload: Mapping[str, Any],
    symbol: str,
    portfolio_policy: Mapping[str, Any],
) -> None:
    if not payload:
        raise DispatchError(f"{stage} assessment must not be empty")
    if payload.get("symbol") not in {None, symbol}:
        raise DispatchError(f"{stage} assessment symbol mismatch")
    if stage == "reveal":
        required = {
            "challenger_required",
            "challenger_reasons",
            "claim_reviews",
            "underwriting_status",
            "reason_codes",
            "portfolio_candidate",
        }
        if set(payload) != required:
            raise DispatchError("reveal assessment fields do not match contract")
        if not isinstance(payload["challenger_required"], bool):
            raise DispatchError("challenger_required must be boolean")
        for field in ("challenger_reasons", "claim_reviews", "reason_codes"):
            if not isinstance(payload[field], list):
                raise DispatchError(f"{field} must be an array")
        _validate_portfolio_candidate(payload["portfolio_candidate"], symbol, portfolio_policy)
    if stage == "arbitration":
        required = {
            "underwriting_status",
            "reason_codes",
            "claim_reviews",
            "portfolio_candidate",
        }
        if set(payload) != required:
            raise DispatchError("arbitration fields do not match contract")
        _validate_portfolio_candidate(payload["portfolio_candidate"], symbol, portfolio_policy)


def _validate_portfolio_candidate(
    payload: Any, symbol: str, policy: Mapping[str, Any]
) -> None:
    if not isinstance(payload, Mapping) or payload.get("symbol") != symbol:
        raise DispatchError(f"portfolio candidate symbol mismatch: {symbol}")
    try:
        build_model_portfolio([payload], policy=policy)
    except ValueError as exc:
        raise DispatchError(f"invalid portfolio candidate for {symbol}: {exc}") from exc


def _artifact_path(candidate: Mapping[str, Any], run_id: str, stage: str) -> Path:
    names = {
        "blind": "blind-assessment.json",
        "reveal": "reveal-assessment.json",
        "challenger": "challenger-assessment.json",
        "arbitration": "arbitration.json",
    }
    return (
        Path(str(candidate["target_company_dir"]))
        / "underwriting"
        / run_id
        / names[stage]
    )


def _allowed_reads(
    candidate: Mapping[str, Any], run_id: str, stage: str
) -> tuple[Path, ...]:
    company_dir = Path(str(candidate["target_company_dir"]))
    review_dir = company_dir / "underwriting" / run_id
    if stage in {"blind", "challenger"}:
        return (review_dir / "claim-packet.json",)
    if stage == "reveal":
        return (review_dir / "blind-assessment.json", company_dir / "meta.json")
    return (
        review_dir / "reveal-assessment.json",
        review_dir / "challenger-assessment.json",
        company_dir / "meta.json",
    )


def _parse_runner_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = re.search(r"__RESULT__(\{.*\})\s*$", stripped, re.DOTALL)
    raw = match.group(1) if match else stripped
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchError("runner stdout is not a JSON object") from exc
    if not isinstance(payload, dict):
        raise DispatchError("runner stdout must contain a JSON object")
    return payload


def _read_json_object(path: Path) -> dict[str, Any]:
    verify_sealed(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DispatchError(f"invalid sealed JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"sealed artifact must be an object: {path}")
    return value


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DispatchError("dispatch timestamp must include a UTC offset")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--runner", required=True, help="JSON-producing runner executable")
    parser.add_argument("--runner-arg", action="append", default=[])
    parser.add_argument("--runs-root", default="automation/runs")
    parser.add_argument("--policy-root", default="policies")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--lease", type=int, default=3900)
    parser.add_argument("--owner", default="review-dispatch")
    args = parser.parse_args(argv)
    dispatcher = ReviewDispatcher(
        runs_root=args.runs_root,
        policy_root=args.policy_root,
        runner=SubprocessRunner([args.runner, *args.runner_arg]),
        owner=args.owner,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
        lease_seconds=args.lease,
    )
    try:
        result = dispatcher.dispatch(args.run_id, now=dt.datetime.now().astimezone())
    except (DispatchError, ReviewStoreError, PromptBuildError) as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": "review_dispatch_failed", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": not result.failed,
                "run_id": result.run_id,
                "stage": result.stage,
                "status": result.status,
                "completed": result.completed,
                "failed": result.failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
