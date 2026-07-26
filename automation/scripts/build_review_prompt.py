"""Render phase-isolated prompts for underwriting review agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading_os.research_assets.models import PolicyKind  # noqa: E402
from trading_os.research_assets.policy_snapshot import (  # noqa: E402
    PolicySnapshotError,
    ReviewPolicySnapshot,
    load_review_policy_snapshot,
)
from trading_os.research_assets.review_store import ReviewRunStore  # noqa: E402
from trading_os.research_assets.sealing import verify_sealed  # noqa: E402


class PromptBuildError(ValueError):
    """Raised when a phase prompt cannot be built without crossing data boundaries."""


TEMPLATES = {
    "blind": "_underwriting_blind_prompt.md",
    "reveal": "_underwriting_reveal_prompt.md",
    "challenger": "_challenger_prompt.md",
    "arbitration": "_arbitration_prompt.md",
    "synthesis": "_portfolio_synthesis_prompt.md",
}
REQUIRED_FIELDS = {
    "blind": {
        "company_name",
        "symbol",
        "output_path",
        "claim_packet",
        "packet_sha256",
        "underwriting_policy",
    },
    "reveal": {
        "company_name",
        "symbol",
        "output_path",
        "blind_assessment",
        "blind_assessment_sha256",
        "prior_report_text",
        "prior_research_claims",
        "underwriting_policy",
    },
    "challenger": {
        "company_name",
        "symbol",
        "output_path",
        "claim_packet",
        "packet_sha256",
        "underwriting_policy",
    },
    "arbitration": {
        "company_name",
        "symbol",
        "output_path",
        "primary_review",
        "challenger_assessment",
        "packet_sha256",
        "input_artifact_sha256s",
        "prior_research_claims",
        "underwriting_policy",
    },
    "synthesis": {
        "run_id",
        "output_path",
        "quote_snapshot",
        "portfolio",
        "portfolio_policy",
    },
}
JSON_FIELDS = {
    "claim_packet",
    "underwriting_policy",
    "blind_assessment",
    "prior_research_claims",
    "primary_review",
    "challenger_assessment",
    "quote_snapshot",
    "portfolio",
    "portfolio_policy",
    "input_artifact_sha256s",
}
REPORT_META_RE = re.compile(
    r"\A<!-- trading-os-report-meta\r?\n(?P<meta>.*?)\r?\n-->\r?\n",
    re.DOTALL,
)


def render_prompt(
    stage: str,
    context: Mapping[str, Any],
    *,
    templates_root: str | Path | None = None,
) -> str:
    if stage not in TEMPLATES:
        raise PromptBuildError(f"unsupported prompt stage: {stage}")
    if not isinstance(context, Mapping):
        raise PromptBuildError("prompt context must be an object")
    required = REQUIRED_FIELDS[stage]
    if set(context) != required:
        raise PromptBuildError(
            "prompt context fields do not match stage contract; "
            f"missing={sorted(required - set(context))}, "
            f"unknown={sorted(set(context) - required)}"
        )
    root = Path(templates_root) if templates_root is not None else Path(__file__).parent
    template = (root / TEMPLATES[stage]).read_text(encoding="utf-8")
    replacements = {
        "company_name": "COMPANY_NAME",
        "symbol": "SYMBOL",
        "output_path": "OUTPUT_PATH",
        "claim_packet": "CLAIM_PACKET_JSON",
        "underwriting_policy": "UNDERWRITING_POLICY_JSON",
        "blind_assessment": "BLIND_ASSESSMENT_JSON",
        "blind_assessment_sha256": "BLIND_ASSESSMENT_SHA256",
        "packet_sha256": "PACKET_SHA256",
        "prior_report_text": "PRIOR_REPORT_TEXT",
        "prior_research_claims": "PRIOR_RESEARCH_CLAIMS_JSON",
        "primary_review": "PRIMARY_REVIEW_JSON",
        "challenger_assessment": "CHALLENGER_ASSESSMENT_JSON",
        "input_artifact_sha256s": "INPUT_ARTIFACT_SHA256S_JSON",
        "run_id": "RUN_ID",
        "quote_snapshot": "QUOTE_SNAPSHOT_JSON",
        "portfolio": "PORTFOLIO_JSON",
        "portfolio_policy": "PORTFOLIO_POLICY_JSON",
    }
    prompt = template
    for field in sorted(required):
        value = context[field]
        rendered = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            if field in JSON_FIELDS
            else _require_text(value, field)
        )
        prompt = prompt.replace(f"{{{{{replacements[field]}}}}}", rendered)
    if "{{" in prompt or "}}" in prompt:
        raise PromptBuildError(f"unresolved placeholder in {stage} prompt")
    return prompt


def build_run_prompt(
    *,
    stage: str,
    run_id: str,
    symbol: str,
    runs_root: str | Path,
    policy_root: str | Path,
) -> str:
    store = ReviewRunStore(runs_root)
    state = store.load_run(run_id)
    snapshot = _load_policy_snapshot(
        runs_root=runs_root,
        run_id=run_id,
        state=state,
    )
    candidate = next(
        (item for item in store.read_candidates(run_id) if item["symbol"] == symbol),
        None,
    )
    if candidate is None:
        raise PromptBuildError(f"symbol is not in frozen candidate set: {symbol}")
    company_dir = Path(candidate["target_company_dir"])
    review_dir = company_dir / "underwriting" / run_id
    policy = _require_snapshot_policy(snapshot, PolicyKind.UNDERWRITING)
    common = {
        "company_name": candidate["name"],
        "symbol": symbol,
        "underwriting_policy": dict(policy["payload"]),
    }
    if stage in {"blind", "challenger"}:
        packet_path = review_dir / "claim-packet.json"
        packet_seal = verify_sealed(packet_path)
        context = {
            **common,
            "output_path": str(review_dir / f"{stage}-assessment.json"),
            "claim_packet": _read_json_object(packet_path),
            "packet_sha256": packet_seal.sha256,
        }
        return render_prompt(stage, context)
    if stage == "reveal":
        blind_path = review_dir / "blind-assessment.json"
        packet_path = review_dir / "claim-packet.json"
        blind_seal = verify_sealed(blind_path)
        packet = _read_json_object(packet_path)
        _assert_frozen_prior_report(company_dir, packet)
        prior_claims, prior_report_text = load_prior_research(company_dir)
        return render_prompt(
            stage,
            {
                **common,
                "output_path": str(review_dir / "reveal-assessment.json"),
                "blind_assessment": _read_json_object(blind_path),
                "blind_assessment_sha256": blind_seal.sha256,
                "prior_report_text": prior_report_text,
                "prior_research_claims": prior_claims,
            },
        )
    if stage == "arbitration":
        blind_path = review_dir / "blind-assessment.json"
        reveal_path = review_dir / "reveal-assessment.json"
        challenger_path = review_dir / "challenger-assessment.json"
        packet_path = review_dir / "claim-packet.json"
        primary_evaluation_path = review_dir / "primary-evaluation.json"
        challenger_evaluation_path = review_dir / "challenger-evaluation.json"
        blind_seal = verify_sealed(blind_path)
        reveal_seal = verify_sealed(reveal_path)
        challenger_seal = verify_sealed(challenger_path)
        packet_seal = verify_sealed(packet_path)
        primary_evaluation_seal = verify_sealed(primary_evaluation_path)
        challenger_evaluation_seal = verify_sealed(challenger_evaluation_path)
        packet = _read_json_object(packet_path)
        _assert_frozen_prior_report(company_dir, packet)
        prior_claims, _ = load_prior_research(company_dir)
        return render_prompt(
            stage,
            {
                **common,
                "output_path": str(review_dir / "arbitration.json"),
                "primary_review": {
                    "blind_assessment": _read_json_object(blind_path),
                    "reveal_assessment": _read_json_object(reveal_path),
                },
                "challenger_assessment": _read_json_object(challenger_path),
                "packet_sha256": packet_seal.sha256,
                "input_artifact_sha256s": {
                    "claim_packet": packet_seal.sha256,
                    "blind_assessment": blind_seal.sha256,
                    "reveal_assessment": reveal_seal.sha256,
                    "primary_evaluation": primary_evaluation_seal.sha256,
                    "challenger_assessment": challenger_seal.sha256,
                    "challenger_evaluation": challenger_evaluation_seal.sha256,
                    "policy_snapshot": snapshot.sha256,
                },
                "prior_research_claims": prior_claims,
            },
        )
    raise PromptBuildError("synthesis prompt requires batch artifacts, not a company symbol")


def build_synthesis_prompt(
    *,
    run_id: str,
    runs_root: str | Path,
    research_root: str | Path,
    policy_root: str | Path,
    output_path: str | Path,
) -> str:
    store = ReviewRunStore(runs_root)
    state = store.load_run(run_id)
    snapshot = _load_policy_snapshot(
        runs_root=runs_root,
        run_id=run_id,
        state=state,
    )
    if state["status"] not in {"synthesizing", "completed"}:
        raise PromptBuildError(
            "synthesis prompt requires a machine portfolio built from completed company reviews"
        )
    batch_dir = Path(research_root) / "batches" / run_id
    quotes_path = batch_dir / "quotes.json"
    portfolio_path = batch_dir / "portfolio.json"
    if verify_sealed(quotes_path).artifact_type != "quote_snapshot":
        raise PromptBuildError("quote artifact type is invalid")
    if verify_sealed(portfolio_path).artifact_type != "model_portfolio":
        raise PromptBuildError("portfolio artifact type is invalid")
    policy = _require_snapshot_policy(snapshot, PolicyKind.PORTFOLIO)
    portfolio = _read_json_object(portfolio_path)
    if (
        "run_id" in portfolio
        or "policy_versions" in portfolio
        or "policy_snapshot_sha256" in portfolio
    ) and portfolio.get("policy_snapshot_sha256") != snapshot.sha256:
        raise PromptBuildError(
            "portfolio policy_snapshot_sha256 does not match the review snapshot"
        )
    return render_prompt(
        "synthesis",
        {
            "run_id": run_id,
            "output_path": str(output_path),
            "quote_snapshot": json.loads(quotes_path.read_text(encoding="utf-8")),
            "portfolio": portfolio,
            "portfolio_policy": dict(policy["payload"]),
        },
    )


def load_prior_research(company_dir: Path) -> tuple[dict[str, Any], str]:
    meta = _read_json_object(company_dir / "meta.json")
    latest = meta.get("reports", {}).get("latest")
    if not isinstance(latest, str):
        raise PromptBuildError("company has no latest report")
    report_path = company_dir / latest
    report_text = report_path.read_text(encoding="utf-8-sig")
    match = REPORT_META_RE.match(report_text)
    if match is None:
        raise PromptBuildError("latest report has no v2 front metadata")
    front = json.loads(match.group("meta"))
    claims_paths: list[Path] = []
    for relative in front["sealed_artifacts"]:
        path = company_dir / relative
        if verify_sealed(path).artifact_type == "research_claims":
            claims_paths.append(path)
    if len(claims_paths) != 1:
        raise PromptBuildError("latest report must reference one research_claims artifact")
    return _read_json_object(claims_paths[0]), report_text


def _assert_frozen_prior_report(
    company_dir: Path,
    claim_packet: Mapping[str, Any],
) -> None:
    meta = _read_json_object(company_dir / "meta.json")
    latest = meta.get("reports", {}).get("latest")
    if not isinstance(latest, str):
        raise PromptBuildError("company has no latest report")
    expected = claim_packet.get("source_report_sha256")
    actual = hashlib.sha256((company_dir / latest).read_bytes()).hexdigest()
    if expected != actual:
        raise PromptBuildError(
            "latest prior report drifted from the frozen claim packet"
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PromptBuildError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PromptBuildError(f"JSON artifact must be an object: {path}")
    return value


def _load_policy_snapshot(
    *,
    runs_root: str | Path,
    run_id: str,
    state: Mapping[str, Any],
) -> ReviewPolicySnapshot:
    try:
        return load_review_policy_snapshot(
            runs_root=runs_root,
            run_id=run_id,
            state=state,
        )
    except PolicySnapshotError as exc:
        raise PromptBuildError(str(exc)) from exc


def _require_snapshot_policy(
    snapshot: ReviewPolicySnapshot,
    kind: PolicyKind,
) -> Mapping[str, Any]:
    try:
        return snapshot.require_kind(kind)
    except PolicySnapshotError as exc:
        raise PromptBuildError(str(exc)) from exc


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptBuildError(f"{label} must be a non-empty string")
    return value.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("blind", "reveal", "challenger", "arbitration"))
    parser.add_argument("run_id")
    parser.add_argument("symbol")
    parser.add_argument("--runs-root", default="automation/runs")
    parser.add_argument("--policy-root", default="policies")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    prompt = build_run_prompt(
        stage=args.stage,
        run_id=args.run_id,
        symbol=args.symbol,
        runs_root=args.runs_root,
        policy_root=args.policy_root,
    )
    if args.out is None:
        sys.stdout.write(prompt)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(prompt, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
