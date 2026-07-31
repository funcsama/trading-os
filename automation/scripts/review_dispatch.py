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
    load_prior_research,
)
from trading_os.research_assets.models import (  # noqa: E402
    PolicyKind,
    ReviewRunStatus,
    UnderwritingStatus,
)
from trading_os.research_assets.policy_snapshot import (  # noqa: E402
    PolicySnapshotError,
    ReviewPolicySnapshot,
    load_review_policy_snapshot,
)
from trading_os.research_assets.portfolio import (  # noqa: E402
    POLICY_KEYS,
    activation_price,
    build_model_portfolio,
    portfolio_candidate_core_sha256,
)
from trading_os.research_assets.review_store import (  # noqa: E402
    ReviewRunStore,
    ReviewStoreError,
)
from trading_os.research_assets.review_workflow import (  # noqa: E402
    ReviewWorkflowError,
    request_challenger_budget,
    require_review_budget_approval,
    verify_review_intake,
)
from trading_os.research_assets.sealing import seal_json, verify_sealed  # noqa: E402
from trading_os.research_assets.underwriting import (  # noqa: E402
    UnderwritingEvaluation,
)
from trading_os.research_assets.underwriting_contract import (  # noqa: E402
    ENVELOPE_KEYS,
    MachineUnderwritingResult,
    evaluate_assessment_envelope,
)


class DispatchError(ValueError):
    """Raised when a review stage cannot be safely dispatched."""


ARBITRATION_INPUT_KEYS = {
    "claim_packet",
    "blind_assessment",
    "reveal_assessment",
    "primary_evaluation",
    "challenger_assessment",
    "challenger_evaluation",
    "policy_snapshot",
}
ARBITRATION_ENVELOPE_KEYS = ENVELOPE_KEYS | {"input_artifact_sha256s"}
HARD_ADVERSE_RISK_FLAGS = {
    "governance_material_doubt",
    "permanent_loss_risk",
}


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


@dataclass(frozen=True, slots=True)
class _ChallengeResolution:
    result: MachineUnderwritingResult
    candidate_source_stage: str
    candidate_envelope: Mapping[str, Any]


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


class DraftDirectoryRunner:
    """Consume explicitly reviewed, unsealed stage drafts from one directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def run(self, task: AgentTask) -> AgentResult:
        path = self.root / (
            f"{task.stage}-{task.symbol.replace(':', '-')}.draft.json"
        )
        try:
            payload = _read_unsealed_json_object(path)
        except (OSError, DispatchError) as exc:
            return AgentResult(ok=False, error=f"draft unavailable: {path}: {exc}")
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
        try:
            verify_review_intake(runs_root=self.runs_root, run_id=run_id)
        except ReviewWorkflowError as exc:
            raise DispatchError(str(exc)) from exc
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
        if status in {
            ReviewRunStatus.CHALLENGING.value,
            ReviewRunStatus.PORTFOLIO_CHALLENGING.value,
        }:
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
            primary_results: dict[str, MachineUnderwritingResult] = {}
            preliminary_candidates: list[dict[str, Any]] = []
            machine_failed: list[tuple[str, str]] = []
            for item in candidates:
                symbol = str(item["symbol"])
                try:
                    result = self._evaluate_artifact(
                        item,
                        run_id,
                        "blind",
                        proposed_top_five=False,
                        challenger_completed=False,
                        evaluated_at=verify_sealed(
                            _artifact_path(item, run_id, "reveal")
                        ).sealed_at,
                    )
                    primary_results[symbol] = result
                    preliminary_candidates.append(
                        self._build_portfolio_candidate(
                            item,
                            run_id,
                            _read_json_object(
                                _artifact_path(item, run_id, "blind")
                            ),
                            result.evaluation,
                            independent_challenger_completed=False,
                            source_machine_decision_sha256="0" * 64,
                        )
                    )
                except Exception as exc:
                    machine_failed.append((symbol, str(exc)))
            if machine_failed:
                failed.extend(machine_failed)
            proposed_top_five = (
                set(
                    build_model_portfolio(
                        preliminary_candidates,
                        policy=self._portfolio_policy(run_id),
                    ).challenger_required_symbols
                )
                if not failed
                else set()
            )
            if not failed:
                needs_challenger: list[Mapping[str, Any]] = []
                for item in candidates:
                    symbol = str(item["symbol"])
                    result = primary_results[symbol]
                    evaluation_time = verify_sealed(
                        _artifact_path(item, run_id, "reveal")
                    ).sealed_at
                    if symbol in proposed_top_five:
                        result = self._evaluate_artifact(
                            item,
                            run_id,
                            "blind",
                            proposed_top_five=True,
                            challenger_completed=False,
                            evaluated_at=evaluation_time,
                        )
                    candidate_payload = self._build_portfolio_candidate(
                        item,
                        run_id,
                        _read_json_object(
                            _artifact_path(item, run_id, "blind")
                        ),
                        result.evaluation,
                        independent_challenger_completed=False,
                        source_machine_decision_sha256="0" * 64,
                    )
                    self._seal_machine_evaluation(
                        item,
                        run_id,
                        result,
                        source_stage="blind",
                        filename="primary-evaluation.json",
                        proposed_top_five=symbol in proposed_top_five,
                        challenger_completed=False,
                        evaluated_at=evaluation_time,
                        candidate_source_stage="blind",
                        portfolio_candidate_core_sha256=(
                            portfolio_candidate_core_sha256(candidate_payload)
                        ),
                    )
                    candidate_payload["source_machine_decision_sha256"] = (
                        verify_sealed(
                            _machine_artifact_path(
                                item,
                                run_id,
                                "primary-evaluation.json",
                            )
                        ).sha256
                    )
                    self._seal_portfolio_candidate(
                        item,
                        run_id,
                        candidate_payload,
                        now=now,
                        final=False,
                    )
                    if (
                        result.evaluation.status
                        == UnderwritingStatus.NEEDS_CHALLENGER.value
                    ):
                        needs_challenger.append(item)
                next_status = (
                    ReviewRunStatus.CHALLENGING.value
                    if needs_challenger
                    else ReviewRunStatus.COMPANY_REVIEWS_COMPLETE.value
                )
                if (
                    needs_challenger
                    and state["intake"]["mode"] == "underwriting_approval"
                ):
                    try:
                        request_challenger_budget(
                            runs_root=self.runs_root,
                            run_id=run_id,
                            symbols=[
                                str(item["symbol"]) for item in needs_challenger
                            ],
                            trigger="company_underwriting_trigger",
                            requested_by=self.owner,
                            requested_at=now,
                        )
                    except ReviewWorkflowError as exc:
                        raise DispatchError(str(exc)) from exc
                state = self.store.transition(
                    run_id,
                    next_status,
                    actor=self.owner,
                    at=now,
                )
            else:
                state = self.store.load_run(run_id)
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
        state = self.store.load_run(run_id)
        portfolio_round = (
            state["status"] == ReviewRunStatus.PORTFOLIO_CHALLENGING.value
        )
        manager_bound = state["intake"]["mode"] == "underwriting_approval"
        budget_approval = None
        if manager_bound:
            trigger = (
                "portfolio_top_five"
                if portfolio_round
                else "company_underwriting_trigger"
            )
            try:
                budget_approval = require_review_budget_approval(
                    runs_root=self.runs_root,
                    run_id=run_id,
                    budget_stage="challenger",
                    trigger=trigger,
                    executor=self.owner,
                )
            except ReviewWorkflowError as exc:
                raise DispatchError(str(exc)) from exc
        portfolio_request = (
            _load_portfolio_challenger_request(self.runs_root, run_id)
            if portfolio_round and not manager_bound
            else None
        )
        if (
            portfolio_request is not None
            and portfolio_request["policy_snapshot_sha256"]
            != self._policy_snapshot(run_id).sha256
        ):
            raise DispatchError(
                "portfolio challenger request policy snapshot mismatch"
            )
        requested_items = (
            {
                str(item["symbol"]): item
                for item in budget_approval["approval"]["items"]
            }
            if budget_approval is not None
            else None
        )
        requested_records = (
            {
                str(item["symbol"]): str(item["primary_candidate_sha256"])
                for item in portfolio_request["candidates"]
            }
            if portfolio_request is not None
            else (
                {
                    symbol: str(item["candidate"]["sha256"])
                    for symbol, item in requested_items.items()
                }
                if requested_items is not None
                else None
            )
        )
        candidates = [
            item
            for item in self.store.read_candidates(run_id)
            if (
                str(item["symbol"]) in requested_records
                if requested_records is not None
                else _read_json_object(
                    _machine_artifact_path(
                        item,
                        run_id,
                        "primary-evaluation.json",
                    )
                )["challenger_required"]
            )
        ]
        if not candidates:
            raise DispatchError("challenging state has no requested candidates")
        if requested_records is not None:
            for item in candidates:
                symbol = str(item["symbol"])
                candidate_path = (
                    (
                        self.runs_root.parent.parent
                        / str(requested_items[symbol]["candidate"]["path"])
                    ).resolve()
                    if requested_items is not None
                    else _portfolio_candidate_path(item, run_id, final=False)
                )
                sealed = verify_sealed(candidate_path)
                if (
                    sealed.artifact_type != "portfolio_candidate"
                    or sealed.sha256 != requested_records[symbol]
                ):
                    raise DispatchError(
                        f"portfolio challenger candidate hash mismatch: {symbol}"
                    )

        def work(item: Mapping[str, Any]) -> str:
            self._run_stage(run_id, item, "challenger", now=now)
            primary = _read_json_object(
                _machine_artifact_path(item, run_id, "primary-evaluation.json")
            )
            proposed_top_five = (
                portfolio_round or bool(primary["proposed_top_five"])
            )
            challenger = self._evaluate_artifact(
                item,
                run_id,
                "challenger",
                proposed_top_five=proposed_top_five,
                challenger_completed=False,
                evaluated_at=verify_sealed(
                    _artifact_path(item, run_id, "challenger")
                ).sealed_at,
            )
            self._seal_machine_evaluation(
                item,
                run_id,
                challenger,
                source_stage="challenger",
                filename="challenger-evaluation.json",
                proposed_top_five=proposed_top_five,
                challenger_completed=False,
                evaluated_at=verify_sealed(
                    _artifact_path(item, run_id, "challenger")
                ).sealed_at,
            )
            self._run_stage(run_id, item, "arbitration", now=now)
            resolution = self._resolve_challenge(
                item,
                run_id,
                proposed_top_five=proposed_top_five,
            )
            final = resolution.result
            candidate_payload = self._build_portfolio_candidate(
                item,
                run_id,
                resolution.candidate_envelope,
                final.evaluation,
                independent_challenger_completed=True,
                source_machine_decision_sha256="0" * 64,
            )
            self._seal_machine_evaluation(
                item,
                run_id,
                final,
                source_stage="arbitration",
                filename="final-underwriting-decision.json",
                proposed_top_five=proposed_top_five,
                challenger_completed=True,
                evaluated_at=verify_sealed(
                    _artifact_path(item, run_id, "arbitration")
                ).sealed_at,
                candidate_source_stage=resolution.candidate_source_stage,
                portfolio_candidate_core_sha256=(
                    portfolio_candidate_core_sha256(candidate_payload)
                ),
            )
            candidate_payload["source_machine_decision_sha256"] = (
                verify_sealed(
                    _machine_artifact_path(
                        item,
                        run_id,
                        "final-underwriting-decision.json",
                    )
                ).sha256
            )
            self._seal_portfolio_candidate(
                item,
                run_id,
                candidate_payload,
                now=now,
                final=True,
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
            self._validate_existing_stage_artifact(
                candidate,
                run_id,
                stage,
                output_path,
            )
            return symbol
        if output_path.exists():
            self._validate_existing_stage_artifact(
                candidate,
                run_id,
                stage,
                output_path,
            )
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
            _validate_stage_payload(
                stage,
                payload,
                symbol,
                run_id=run_id,
                expected_packet_sha256=verify_sealed(
                    _artifact_path(candidate, run_id, "claim_packet")
                ).sha256,
                blind_assessment_sha256=(
                    verify_sealed(_artifact_path(candidate, run_id, "blind")).sha256
                    if stage == "reveal"
                    else None
                ),
                arbitration_input_sha256s=(
                    self._arbitration_input_sha256s(candidate, run_id)
                    if stage == "arbitration"
                    else None
                ),
            )
            if stage in {"blind", "challenger", "arbitration"}:
                self._evaluate_payload(
                    candidate,
                    run_id,
                    payload,
                    proposed_top_five=False,
                    challenger_completed=stage == "arbitration",
                    prior_fair_value_range=None,
                    evaluated_at=now,
                )
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

    def _validate_existing_stage_artifact(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        stage: str,
        output_path: Path,
    ) -> None:
        sealed = verify_sealed(output_path)
        if sealed.artifact_type != f"{stage}_assessment":
            raise DispatchError(f"{stage} artifact type is invalid")
        payload = _read_json_object(output_path)
        _validate_stage_payload(
            stage,
            payload,
            str(candidate["symbol"]),
            run_id=run_id,
            expected_packet_sha256=verify_sealed(
                _artifact_path(candidate, run_id, "claim_packet")
            ).sha256,
            blind_assessment_sha256=(
                verify_sealed(_artifact_path(candidate, run_id, "blind")).sha256
                if stage == "reveal"
                else None
            ),
            arbitration_input_sha256s=(
                self._arbitration_input_sha256s(candidate, run_id)
                if stage == "arbitration"
                else None
            ),
        )

    def _evaluate_payload(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        payload: Mapping[str, Any],
        *,
        proposed_top_five: bool,
        challenger_completed: bool,
        prior_fair_value_range: list[float] | None,
        evaluated_at: dt.datetime,
    ) -> MachineUnderwritingResult:
        packet_path = _artifact_path(candidate, run_id, "claim_packet")
        packet_seal = verify_sealed(packet_path)
        if packet_seal.artifact_type != "claim_packet":
            raise DispatchError("claim packet artifact type is invalid")
        underwriting_policy = self._policy_snapshot(run_id).require_kind(
            PolicyKind.UNDERWRITING
        )
        try:
            machine_envelope = {
                key: payload[key]
                for key in ENVELOPE_KEYS
            }
            result = evaluate_assessment_envelope(
                machine_envelope,
                expected_symbol=str(candidate["symbol"]),
                expected_review_id=run_id,
                expected_packet_sha256=packet_seal.sha256,
                claim_packet=_read_json_object(packet_path),
                underwriting_policy=underwriting_policy["payload"],
                evaluated_at=evaluated_at,
                prior_fair_value_range=prior_fair_value_range,
                proposed_top_five=proposed_top_five,
                challenger_completed=challenger_completed,
            )
        except ValueError as exc:
            raise DispatchError(
                f"machine underwriting rejected {candidate['symbol']}: {exc}"
            ) from exc
        company_meta = _read_unsealed_json_object(
            Path(candidate["target_company_dir"]) / "meta.json"
        )
        expected_currency = company_meta.get("identity", {}).get("currency")
        actual_currency = payload["portfolio_inputs"]["return_model"]["currency"]
        if actual_currency != expected_currency:
            raise DispatchError(
                f"return model currency mismatch for {candidate['symbol']}"
            )
        return result

    def _evaluate_artifact(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        stage: str,
        *,
        proposed_top_five: bool,
        challenger_completed: bool,
        evaluated_at: dt.datetime,
    ) -> MachineUnderwritingResult:
        path = _artifact_path(candidate, run_id, stage)
        sealed = verify_sealed(path)
        if sealed.artifact_type != f"{stage}_assessment":
            raise DispatchError(f"{stage} assessment artifact type is invalid")
        prior_claims, _ = load_prior_research(
            Path(candidate["target_company_dir"])
        )
        prior_range = prior_claims.get("decision", {}).get("fair_value_range")
        if not isinstance(prior_range, list):
            raise DispatchError("prior fair_value_range is unavailable")
        return self._evaluate_payload(
            candidate,
            run_id,
            _read_json_object(path),
            proposed_top_five=proposed_top_five,
            challenger_completed=challenger_completed,
            prior_fair_value_range=prior_range,
            evaluated_at=evaluated_at,
        )

    def _seal_machine_evaluation(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        result: MachineUnderwritingResult,
        *,
        source_stage: str,
        filename: str,
        proposed_top_five: bool,
        challenger_completed: bool,
        evaluated_at: dt.datetime,
        candidate_source_stage: str | None = None,
        portfolio_candidate_core_sha256: str | None = None,
    ) -> None:
        _require_aware(evaluated_at)
        source_path = _artifact_path(candidate, run_id, source_stage)
        source_seal = verify_sealed(source_path)
        source_payload = _read_json_object(source_path)
        economic_source_stage = candidate_source_stage or source_stage
        if economic_source_stage not in {"blind", "challenger"}:
            raise DispatchError(
                "candidate economic source must be blind or challenger"
            )
        economic_source_seal = verify_sealed(
            _artifact_path(candidate, run_id, economic_source_stage)
        )
        if (
            portfolio_candidate_core_sha256 is not None
            and (
                not isinstance(portfolio_candidate_core_sha256, str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    portfolio_candidate_core_sha256,
                )
            )
        ):
            raise DispatchError(
                "portfolio candidate core SHA-256 is invalid"
            )
        evaluation = result.evaluation
        resolved_triggers = (
            list(evaluation.challenger_triggers)
            if challenger_completed and evaluation.status == "passed"
            else []
        )
        unresolved_triggers = (
            []
            if resolved_triggers
            else list(evaluation.challenger_triggers)
        )
        payload = {
            "schema_version": 3,
            "review_id": run_id,
            "symbol": str(candidate["symbol"]),
            "source_stage": source_stage,
            "source_assessment_sha256": source_seal.sha256,
            "candidate_source_stage": economic_source_stage,
            "candidate_source_assessment_sha256": economic_source_seal.sha256,
            "portfolio_candidate_core_sha256": (
                portfolio_candidate_core_sha256
            ),
            "policy_snapshot_sha256": self._policy_snapshot(run_id).sha256,
            "input_artifact_sha256s": dict(
                source_payload.get("input_artifact_sha256s", {})
            ),
            "evaluated_at": evaluated_at.isoformat(),
            "status": evaluation.status,
            "required_return": evaluation.required_return,
            "required_safety_margin": evaluation.required_safety_margin,
            "blockers": list(evaluation.blockers),
            "challenger_triggers": list(evaluation.challenger_triggers),
            "resolved_challenger_triggers": resolved_triggers,
            "unresolved_challenger_triggers": unresolved_triggers,
            "challenger_required": (
                evaluation.status == UnderwritingStatus.NEEDS_CHALLENGER.value
                and not challenger_completed
            ),
            "proposed_top_five": proposed_top_five,
            "challenger_completed": challenger_completed,
            "evidence": {
                "is_valid": result.evidence.is_valid,
                "is_stale": result.evidence.is_stale,
                "blockers": list(result.evidence.blockers),
                "warnings": list(result.evidence.warnings),
            },
        }
        seal_json(
            _machine_artifact_path(candidate, run_id, filename),
            payload,
            artifact_type="machine_underwriting_evaluation",
            sealed_at=evaluated_at,
        )

    def _build_portfolio_candidate(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        envelope: Mapping[str, Any],
        evaluation: UnderwritingEvaluation,
        *,
        independent_challenger_completed: bool,
        source_machine_decision_sha256: str,
    ) -> dict[str, Any]:
        assessment = envelope["assessment"]
        valuation = assessment["valuation"]
        inputs = envelope["portfolio_inputs"]
        price = float(inputs["current_price"])
        bear_value = float(valuation["scenarios"]["bear"])
        reasons = set(evaluation.blockers) | set(evaluation.challenger_triggers)
        reasons.add(f"underwriting_{evaluation.status}")
        policy = self._portfolio_policy(run_id)
        return {
            "symbol": str(candidate["symbol"]),
            "name": str(candidate["name"]),
            "underwriting_status": evaluation.status,
            "evidence_stale": evaluation.status == "stale",
            "independent_challenger_completed": independent_challenger_completed,
            "source_machine_decision_sha256": source_machine_decision_sha256,
            "policy_snapshot_sha256": self._policy_snapshot(run_id).sha256,
            "current_price": price,
            "price_as_of": str(inputs["price_as_of"]),
            "bear_value": bear_value,
            "fair_value_range": list(valuation["fair_value_range"]),
            "buy_zone": list(valuation["buy_zone"]),
            "reduce_zone": list(inputs["reduce_zone"]),
            "confidence": str(assessment["confidence"]),
            "industry": str(inputs["industry"]),
            "economic_risk_clusters": list(inputs["economic_risk_clusters"]),
            "return_model": dict(inputs["return_model"]),
            "bear_case_loss_fraction": max(0.0, (price - bear_value) / price),
            "allowed_loss_weight": policy["max_per_name_loss_weight"],
            "held": False,
            "reason_codes": sorted(reasons),
        }

    def _resolve_challenge(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        *,
        proposed_top_five: bool,
    ) -> _ChallengeResolution:
        primary_time = verify_sealed(
            _artifact_path(candidate, run_id, "reveal")
        ).sealed_at
        challenger_time = verify_sealed(
            _artifact_path(candidate, run_id, "challenger")
        ).sealed_at
        primary = self._evaluate_artifact(
            candidate,
            run_id,
            "blind",
            proposed_top_five=proposed_top_five,
            challenger_completed=False,
            evaluated_at=primary_time,
        )
        challenger = self._evaluate_artifact(
            candidate,
            run_id,
            "challenger",
            proposed_top_five=proposed_top_five,
            challenger_completed=False,
            evaluated_at=challenger_time,
        )
        # A challenger trigger is a workflow gate, not permission for the
        # arbitrator to upgrade either independent assessment. Re-evaluate
        # both sealed independent envelopes after the gate has been fulfilled,
        # then use those results as the best status arbitration may preserve.
        primary_completed = self._evaluate_artifact(
            candidate,
            run_id,
            "blind",
            proposed_top_five=proposed_top_five,
            challenger_completed=True,
            evaluated_at=primary_time,
        )
        challenger_completed = self._evaluate_artifact(
            candidate,
            run_id,
            "challenger",
            proposed_top_five=proposed_top_five,
            challenger_completed=True,
            evaluated_at=challenger_time,
        )
        arbitration = self._evaluate_artifact(
            candidate,
            run_id,
            "arbitration",
            proposed_top_five=proposed_top_five,
            challenger_completed=True,
            evaluated_at=verify_sealed(
                _artifact_path(candidate, run_id, "arbitration")
            ).sealed_at,
        )
        primary_envelope = _read_json_object(
            _artifact_path(candidate, run_id, "blind")
        )
        challenger_envelope = _read_json_object(
            _artifact_path(candidate, run_id, "challenger")
        )
        arbitration_envelope = _read_json_object(
            _artifact_path(candidate, run_id, "arbitration")
        )
        minimum_return = float(
            self._portfolio_policy(run_id)["minimum_expected_annual_return"]
        )
        primary_activation_price = activation_price(
            primary_envelope["portfolio_inputs"]["return_model"],
            minimum_expected_annual_return=minimum_return,
        )
        challenger_activation_price = activation_price(
            challenger_envelope["portfolio_inputs"]["return_model"],
            minimum_expected_annual_return=minimum_return,
        )
        arbitration_activation_price = activation_price(
            arbitration_envelope["portfolio_inputs"]["return_model"],
            minimum_expected_annual_return=minimum_return,
        )
        candidate_source_stage, candidate_envelope = (
            _select_conservative_candidate_envelope(
                primary_envelope,
                challenger_envelope,
                primary_activation_price=primary_activation_price,
                challenger_activation_price=challenger_activation_price,
            )
        )
        primary_midpoint = sum(
            primary_envelope["assessment"]["valuation"]["fair_value_range"]
        ) / 2
        challenger_midpoint = sum(
            challenger_envelope["assessment"]["valuation"]["fair_value_range"]
        ) / 2
        threshold = float(
            self._policy_snapshot(run_id).require_kind(PolicyKind.UNDERWRITING)[
                "payload"
            ][
                "challenger_thresholds"
            ]["old_new_fair_value_midpoint_difference"]
        )
        disagreement = (
            abs(primary_midpoint - challenger_midpoint) / primary_midpoint
            if primary_midpoint > 0
            else float("inf")
        )
        lower_activation_price = min(
            primary_activation_price,
            challenger_activation_price,
        )
        return_model_disagreement = (
            abs(primary_activation_price - challenger_activation_price)
            / lower_activation_price
            if lower_activation_price > 0
            else float("inf")
        )
        material_disagreements = _material_assessment_disagreements(
            primary_envelope,
            challenger_envelope,
            arbitration_envelope,
            prior_claims=primary.prior_claims,
        )
        consensus_adverse_findings = _consensus_adverse_findings(
            primary_envelope,
            challenger_envelope,
            prior_claims=primary.prior_claims,
        )
        arbitration_adverse_findings = _arbitration_adverse_findings(
            arbitration_envelope,
            prior_claims=primary.prior_claims,
        )
        arbitration_economic_boundary_findings = (
            _arbitration_economic_boundary_findings(
                primary_envelope,
                challenger_envelope,
                arbitration_envelope,
                primary_activation_price=primary_activation_price,
                challenger_activation_price=challenger_activation_price,
                arbitration_activation_price=arbitration_activation_price,
            )
        )
        status = _least_favorable_status(
            primary_completed.evaluation.status,
            challenger_completed.evaluation.status,
            arbitration.evaluation.status,
        )
        blocker_set = set(arbitration.evaluation.blockers)
        independent_evidence = (primary.evidence, challenger.evidence)
        if any(not item.is_valid for item in independent_evidence):
            blocker_set.update(
                {"independent_evidence_invalid"}
                | set(primary.evidence.blockers)
                | set(challenger.evidence.blockers)
            )
        primary_machine_blockers = (
            set(primary.evaluation.blockers) - set(primary.evidence.blockers)
        )
        challenger_machine_blockers = (
            set(challenger.evaluation.blockers)
            - set(challenger.evidence.blockers)
        )
        if primary_machine_blockers or challenger_machine_blockers:
            status = "failed"
            blocker_set.update(
                {"independent_machine_validation_failed"}
                | {
                    f"primary_blocker:{code}"
                    for code in primary_machine_blockers
                }
                | {
                    f"challenger_blocker:{code}"
                    for code in challenger_machine_blockers
                }
            )
        if material_disagreements:
            status = "failed"
            blocker_set.update(material_disagreements)
        if consensus_adverse_findings:
            status = "failed"
            blocker_set.update(consensus_adverse_findings)
        if arbitration_adverse_findings:
            status = "failed"
            blocker_set.update(arbitration_adverse_findings)
        if arbitration_economic_boundary_findings:
            status = "failed"
            blocker_set.update(arbitration_economic_boundary_findings)
        if disagreement > threshold:
            status = "failed"
            blocker_set.add("challenger_no_valuation_consensus")
        if return_model_disagreement > threshold:
            status = "failed"
            blocker_set.add("challenger_no_return_model_consensus")
        independent_margins = (
            primary_completed.evaluation.required_safety_margin,
            challenger_completed.evaluation.required_safety_margin,
        )
        final_evaluation = UnderwritingEvaluation(
            status=status,
            required_return=max(
                primary_completed.evaluation.required_return,
                challenger_completed.evaluation.required_return,
            ),
            required_safety_margin=(
                None
                if any(item is None for item in independent_margins)
                else max(item for item in independent_margins if item is not None)
            ),
            blockers=tuple(sorted(blocker_set)),
            challenger_triggers=tuple(
                sorted(
                    set(primary.evaluation.challenger_triggers)
                    | set(arbitration.evaluation.challenger_triggers)
                    | set(challenger.evaluation.challenger_triggers)
                )
            ),
        )
        final_evidence = next(
            (
                item
                for item in (
                    primary.evidence,
                    challenger.evidence,
                    arbitration.evidence,
                )
                if not item.is_valid
            ),
            (
                primary.evidence
                if candidate_source_stage == "blind"
                else challenger.evidence
            ),
        )
        return _ChallengeResolution(
            result=MachineUnderwritingResult(
                evaluation=final_evaluation,
                evidence=final_evidence,
                prior_claims=primary.prior_claims,
            ),
            candidate_source_stage=candidate_source_stage,
            candidate_envelope=candidate_envelope,
        )

    def _seal_portfolio_candidate(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
        payload: Any,
        *,
        now: dt.datetime,
        final: bool,
    ) -> None:
        if not isinstance(payload, Mapping):
            raise DispatchError("portfolio_candidate must be an object")
        normalized = dict(payload)
        _validate_portfolio_candidate(
            normalized,
            str(candidate["symbol"]),
            self._portfolio_policy(run_id),
        )
        seal_json(
            _portfolio_candidate_path(candidate, run_id, final=final),
            normalized,
            artifact_type="portfolio_candidate",
            sealed_at=now,
        )

    def _portfolio_policy(self, run_id: str) -> dict[str, Any]:
        policy = self._policy_snapshot(run_id).require_kind(PolicyKind.PORTFOLIO)
        return {key: policy["payload"][key] for key in POLICY_KEYS}

    def _policy_snapshot(self, run_id: str) -> ReviewPolicySnapshot:
        state = self.store.load_run(run_id)
        try:
            return load_review_policy_snapshot(
                runs_root=self.runs_root,
                run_id=run_id,
                state=state,
            )
        except PolicySnapshotError as exc:
            raise DispatchError(str(exc)) from exc

    def _arbitration_input_sha256s(
        self,
        candidate: Mapping[str, Any],
        run_id: str,
    ) -> dict[str, str]:
        paths = {
            "claim_packet": _artifact_path(candidate, run_id, "claim_packet"),
            "blind_assessment": _artifact_path(candidate, run_id, "blind"),
            "reveal_assessment": _artifact_path(candidate, run_id, "reveal"),
            "primary_evaluation": _machine_artifact_path(
                candidate,
                run_id,
                "primary-evaluation.json",
            ),
            "challenger_assessment": _artifact_path(
                candidate,
                run_id,
                "challenger",
            ),
            "challenger_evaluation": _machine_artifact_path(
                candidate,
                run_id,
                "challenger-evaluation.json",
            ),
        }
        result = {
            label: verify_sealed(path).sha256
            for label, path in paths.items()
        }
        result["policy_snapshot"] = self._policy_snapshot(run_id).sha256
        return result

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


def _select_conservative_candidate_envelope(
    primary: Mapping[str, Any],
    challenger: Mapping[str, Any],
    *,
    primary_activation_price: float,
    challenger_activation_price: float,
) -> tuple[str, Mapping[str, Any]]:
    """Choose one complete independent envelope without field-level mixing."""

    def selection_key(
        stage: str,
        envelope: Mapping[str, Any],
        return_activation_price: float,
    ) -> tuple[float, ...]:
        valuation = envelope["assessment"]["valuation"]
        fair_value_range = valuation["fair_value_range"]
        scenarios = valuation["scenarios"]
        buy_zone = valuation["buy_zone"]
        # A lower 12% activation price means the sealed cash-flow model
        # requires a lower entry price and is therefore the conservative
        # economic source. The remaining fields are deterministic tie-breakers
        # only; the selected envelope is always consumed whole.
        return (
            float(return_activation_price),
            sum(float(item) for item in fair_value_range) / 2,
            float(scenarios["bear"]),
            float(scenarios["base"]),
            float(scenarios["bull"]),
            float(buy_zone[1]),
            float(buy_zone[0]),
            0.0 if stage == "blind" else 1.0,
        )

    choices = (
        (
            selection_key(
                "blind",
                primary,
                primary_activation_price,
            ),
            "blind",
            primary,
        ),
        (
            selection_key(
                "challenger",
                challenger,
                challenger_activation_price,
            ),
            "challenger",
            challenger,
        ),
    )
    _, stage, envelope = min(choices, key=lambda item: item[0])
    return stage, envelope


def _least_favorable_status(*statuses: str) -> str:
    precedence = {
        "passed": 0,
        "needs_challenger": 1,
        "insufficient_evidence": 2,
        "stale": 3,
        "failed": 4,
    }
    unknown = sorted(set(statuses) - set(precedence))
    if unknown:
        raise DispatchError(
            f"unsupported underwriting status in consensus: {unknown}"
        )
    return max(statuses, key=precedence.__getitem__)


def _arbitration_economic_boundary_findings(
    primary: Mapping[str, Any],
    challenger: Mapping[str, Any],
    arbitration: Mapping[str, Any],
    *,
    primary_activation_price: float,
    challenger_activation_price: float,
    arbitration_activation_price: float,
) -> set[str]:
    """Reject economic values invented outside both independent envelopes."""

    def economic_values(
        envelope: Mapping[str, Any],
        return_activation_price: float,
    ) -> dict[str, float]:
        valuation = envelope["assessment"]["valuation"]
        scenarios = valuation["scenarios"]
        fair_value_range = valuation["fair_value_range"]
        buy_zone = valuation["buy_zone"]
        reduce_zone = envelope["portfolio_inputs"]["reduce_zone"]
        return {
            "return_activation_price": float(return_activation_price),
            "bear_value": float(scenarios["bear"]),
            "base_value": float(scenarios["base"]),
            "bull_value": float(scenarios["bull"]),
            "fair_value_low": float(fair_value_range[0]),
            "fair_value_high": float(fair_value_range[1]),
            "buy_zone_low": float(buy_zone[0]),
            "buy_zone_high": float(buy_zone[1]),
            "reduce_zone_low": float(reduce_zone[0]),
            "reduce_zone_high": float(reduce_zone[1]),
        }

    primary_values = economic_values(primary, primary_activation_price)
    challenger_values = economic_values(
        challenger,
        challenger_activation_price,
    )
    arbitration_values = economic_values(
        arbitration,
        arbitration_activation_price,
    )
    details = {
        f"arbitration_economic_outside_independent_bounds:{field}"
        for field, arbitration_value in arbitration_values.items()
        if not (
            min(primary_values[field], challenger_values[field]) - 1e-9
            <= arbitration_value
            <= max(primary_values[field], challenger_values[field]) + 1e-9
        )
    }
    if details:
        details.add("arbitration_economics_outside_independent_bounds")
    return details


def _consensus_adverse_findings(
    primary: Mapping[str, Any],
    challenger: Mapping[str, Any],
    *,
    prior_claims: Mapping[str, str],
) -> set[str]:
    primary_assessment = primary["assessment"]
    challenger_assessment = challenger["assessment"]
    findings = {
        f"consensus_risk_flag:{flag}"
        for flag, active in primary_assessment["risk_flags"].items()
        if (
            flag in HARD_ADVERSE_RISK_FLAGS
            and active
            and challenger_assessment["risk_flags"][flag]
        )
    }
    primary_claims = {
        item["claim_id"]: item["result"]
        for item in primary_assessment["claim_reviews"]
    }
    challenger_claims = {
        item["claim_id"]: item["result"]
        for item in challenger_assessment["claim_reviews"]
    }
    findings.update(
        f"consensus_investment_claim_disproven:{claim_id}"
        for claim_id, category in prior_claims.items()
        if (
            category == "investment"
            and primary_claims[claim_id] == "disproven"
            and challenger_claims[claim_id] == "disproven"
        )
    )
    return findings


def _arbitration_adverse_findings(
    arbitration: Mapping[str, Any],
    *,
    prior_claims: Mapping[str, str],
) -> set[str]:
    assessment = arbitration["assessment"]
    findings = {
        f"arbitration_risk_flag:{flag}"
        for flag, active in assessment["risk_flags"].items()
        if flag in HARD_ADVERSE_RISK_FLAGS and active
    }
    claims = {
        item["claim_id"]: item["result"]
        for item in assessment["claim_reviews"]
    }
    findings.update(
        f"arbitration_investment_claim_disproven:{claim_id}"
        for claim_id, category in prior_claims.items()
        if category == "investment" and claims[claim_id] == "disproven"
    )
    return findings


def _material_assessment_disagreements(
    primary: Mapping[str, Any],
    challenger: Mapping[str, Any],
    arbitration: Mapping[str, Any],
    *,
    prior_claims: Mapping[str, str],
) -> set[str]:
    primary_assessment = primary["assessment"]
    challenger_assessment = challenger["assessment"]
    arbitration_assessment = arbitration["assessment"]
    disagreements: set[str] = set()
    primary_risks = primary_assessment["risk_flags"]
    challenger_risks = challenger_assessment["risk_flags"]
    for flag in sorted(primary_risks):
        if primary_risks[flag] != challenger_risks[flag]:
            disagreements.add(f"unresolved_risk_flag:{flag}")
    primary_claims = {
        item["claim_id"]: item["result"]
        for item in primary_assessment["claim_reviews"]
    }
    challenger_claims = {
        item["claim_id"]: item["result"]
        for item in challenger_assessment["claim_reviews"]
    }
    arbitration_claims = {
        item["claim_id"]: item["result"]
        for item in arbitration_assessment["claim_reviews"]
    }
    for claim_id, category in prior_claims.items():
        if (
            category == "investment"
            and not _claim_result_is_conservatively_resolved(
                primary_claims[claim_id],
                challenger_claims[claim_id],
                arbitration_claims[claim_id],
            )
        ):
            disagreements.add(f"unresolved_investment_claim:{claim_id}")
    return disagreements


def _claim_result_is_conservatively_resolved(
    primary_result: str,
    challenger_result: str,
    arbitration_result: str,
) -> bool:
    """Return whether arbitration preserves the least favorable supported result.

    ``untested`` is epistemic uncertainty rather than a directional finding, so
    it is deliberately incomparable with confirmed/weakened/disproven. An
    arbitrator cannot turn an untested independent result into consensus.
    """

    if "untested" in {primary_result, challenger_result, arbitration_result}:
        return primary_result == challenger_result == arbitration_result
    conservatism = {
        "confirmed": 0,
        "weakened": 1,
        "disproven": 2,
    }
    return conservatism[arbitration_result] >= max(
        conservatism[primary_result],
        conservatism[challenger_result],
    )


def _validate_stage_payload(
    stage: str,
    payload: Mapping[str, Any],
    symbol: str,
    *,
    run_id: str,
    expected_packet_sha256: str,
    blind_assessment_sha256: str | None,
    arbitration_input_sha256s: Mapping[str, str] | None,
) -> None:
    if not payload:
        raise DispatchError(f"{stage} assessment must not be empty")
    if payload.get("symbol") != symbol:
        raise DispatchError(f"{stage} assessment symbol mismatch")
    if stage in {"blind", "challenger"}:
        if set(payload) != ENVELOPE_KEYS:
            raise DispatchError(
                f"{stage} assessment fields do not match machine contract"
            )
        if payload.get("schema_version") != 3:
            raise DispatchError(f"{stage} assessment schema_version must be 3")
        if payload.get("review_id") != run_id:
            raise DispatchError(f"{stage} assessment review_id mismatch")
        if payload.get("packet_sha256") != expected_packet_sha256:
            raise DispatchError(f"{stage} assessment packet_sha256 mismatch")
    if stage == "arbitration":
        if set(payload) != ARBITRATION_ENVELOPE_KEYS:
            raise DispatchError(
                "arbitration assessment fields do not match machine contract"
            )
        if payload.get("schema_version") != 3:
            raise DispatchError("arbitration assessment schema_version must be 3")
        if payload.get("review_id") != run_id:
            raise DispatchError("arbitration assessment review_id mismatch")
        if payload.get("packet_sha256") != expected_packet_sha256:
            raise DispatchError("arbitration assessment packet_sha256 mismatch")
        links = payload.get("input_artifact_sha256s")
        if (
            not isinstance(links, Mapping)
            or set(links) != ARBITRATION_INPUT_KEYS
            or arbitration_input_sha256s is None
            or dict(links) != dict(arbitration_input_sha256s)
        ):
            raise DispatchError(
                "arbitration input artifact SHA-256 links do not match"
            )
    if stage == "reveal":
        required = {
            "schema_version",
            "review_id",
            "symbol",
            "blind_assessment_sha256",
            "difference_findings",
        }
        if set(payload) != required:
            raise DispatchError("reveal assessment fields do not match contract")
        if payload.get("schema_version") != 3 or payload.get("review_id") != run_id:
            raise DispatchError("reveal assessment identity mismatch")
        if payload.get("blind_assessment_sha256") != blind_assessment_sha256:
            raise DispatchError("reveal blind_assessment_sha256 mismatch")
        findings = payload.get("difference_findings")
        if (
            not isinstance(findings, list)
            or any(not isinstance(item, str) or not item.strip() for item in findings)
        ):
            raise DispatchError("difference_findings must be non-empty strings")


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
        "claim_packet": "claim-packet.json",
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


def _machine_artifact_path(
    candidate: Mapping[str, Any],
    run_id: str,
    filename: str,
) -> Path:
    return (
        Path(str(candidate["target_company_dir"]))
        / "underwriting"
        / run_id
        / filename
    )


def _portfolio_candidate_path(
    candidate: Mapping[str, Any],
    run_id: str,
    *,
    final: bool,
) -> Path:
    filename = (
        "portfolio-candidate.final.json"
        if final
        else "portfolio-candidate.primary.json"
    )
    return (
        Path(str(candidate["target_company_dir"]))
        / "underwriting"
        / run_id
        / filename
    )


def _load_portfolio_challenger_request(
    runs_root: Path,
    run_id: str,
) -> dict[str, Any]:
    request_root = runs_root / run_id / "portfolio_challenger_requests"
    paths = (
        sorted(
            path
            for path in request_root.glob("*.json")
            if not path.name.endswith(".seal.json")
        )
        if request_root.is_dir()
        else []
    )
    if not paths:
        raise DispatchError("portfolio challenging request is missing")
    sealed_requests = []
    for path in paths:
        sealed = verify_sealed(path)
        if sealed.artifact_type != "portfolio_challenger_request":
            raise DispatchError("portfolio challenger request type is invalid")
        sealed_requests.append((sealed.sealed_at, path))
    _, latest_path = max(sealed_requests, key=lambda item: (item[0], str(item[1])))
    payload = _read_json_object(latest_path)
    required = {
        "schema_version",
        "run_id",
        "requested_at",
        "quote_snapshot_sha256",
        "policy_snapshot_sha256",
        "candidates",
    }
    if set(payload) != required or payload.get("schema_version") != 3:
        raise DispatchError("portfolio challenger request contract is invalid")
    if payload.get("run_id") != run_id:
        raise DispatchError("portfolio challenger request run_id mismatch")
    records = payload.get("candidates")
    if not isinstance(records, list) or not records:
        raise DispatchError("portfolio challenger request has no candidates")
    candidates = {
        str(item["symbol"]): str(item["primary_candidate_sha256"])
        for item in records
        if isinstance(item, Mapping)
        and set(item) == {"symbol", "primary_candidate_sha256"}
    }
    if len(candidates) != len(records):
        raise DispatchError("portfolio challenger candidate records are invalid")
    if any(
        len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        for digest in candidates.values()
    ):
        raise DispatchError(
            "portfolio challenger candidate SHA-256 is invalid"
        )
    for label in ("quote_snapshot_sha256", "policy_snapshot_sha256"):
        value = payload.get(label)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise DispatchError(
                f"portfolio challenger request {label} is invalid"
            )
    return payload


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


def _read_unsealed_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise DispatchError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"JSON artifact must be an object: {path}")
    return value


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DispatchError("dispatch timestamp must include a UTC offset")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    runner_group = parser.add_mutually_exclusive_group(required=True)
    runner_group.add_argument(
        "--runner",
        help="JSON-producing runner executable",
    )
    runner_group.add_argument(
        "--draft-root",
        help="directory containing {stage}-{market}-{ticker}.draft.json files",
    )
    parser.add_argument("--runner-arg", action="append", default=[])
    parser.add_argument("--runs-root", default="automation/runs")
    parser.add_argument("--policy-root", default="policies")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--lease", type=int, default=3900)
    parser.add_argument("--owner", default="review-dispatch")
    args = parser.parse_args(argv)
    if args.draft_root and args.runner_arg:
        parser.error("--runner-arg cannot be used with --draft-root")
    runner: Runner = (
        DraftDirectoryRunner(args.draft_root)
        if args.draft_root
        else SubprocessRunner([args.runner, *args.runner_arg])
    )
    dispatcher = ReviewDispatcher(
        runs_root=args.runs_root,
        policy_root=args.policy_root,
        runner=runner,
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
