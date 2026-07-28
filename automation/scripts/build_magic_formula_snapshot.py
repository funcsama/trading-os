from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from trading_os.research_assets.coverage_store import read_jsonl
from trading_os.research_assets.magic_formula import build_magic_formula_snapshot
from trading_os.research_assets.sealing import seal_json

ENDPOINT = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORTS = {
    "income": {
        "name": "RPT_F10_FINANCE_GINCOME",
        "columns": [
            "SECUCODE",
            "SECURITY_CODE",
            "ORG_TYPE",
            "REPORT_DATE",
            "REPORT_TYPE",
            "NOTICE_DATE",
            "UPDATE_DATE",
            "TOTAL_PROFIT",
            "OPERATE_PROFIT",
            "FE_INTEREST_EXPENSE",
            "FINANCE_EXPENSE",
            "INVEST_INCOME",
            "FAIRVALUE_CHANGE_INCOME",
            "ASSET_DISPOSAL_INCOME",
            "OTHER_INCOME",
            "TOTAL_OPERATE_INCOME",
        ],
    },
    "balance": {
        "name": "RPT_F10_FINANCE_GBALANCE",
        "columns": [
            "SECUCODE",
            "SECURITY_CODE",
            "ORG_TYPE",
            "REPORT_DATE",
            "REPORT_TYPE",
            "NOTICE_DATE",
            "UPDATE_DATE",
            "TOTAL_CURRENT_ASSETS",
            "TOTAL_CURRENT_LIAB",
            "FIXED_ASSET",
            "CIP",
            "MONETARYFUNDS",
            "SHORT_LOAN",
            "SHORT_BOND_PAYABLE",
            "NONCURRENT_LIAB_1YEAR",
            "LONG_LOAN",
            "BOND_PAYABLE",
            "LEASE_LIAB",
            "PERPETUAL_BOND_PAYBALE",
            "MINORITY_EQUITY",
            "OTHER_EQUITY_TOOL",
            "PREFERRED_SHARES",
        ],
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and seal a point-in-time non-financial Magic Formula snapshot."
    )
    parser.add_argument("--companies", default="coverage/cn-a/companies.jsonl")
    parser.add_argument(
        "--output", default="automation/magic_formula_snapshot.json"
    )
    parser.add_argument(
        "--raw-dir", default="automation/private-source-cache/magic-formula"
    )
    parser.add_argument("--years", default="2023,2024,2025")
    parser.add_argument("--latest-balance-date")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--at")
    args = parser.parse_args()

    generated_at = (
        dt.datetime.fromisoformat(args.at)
        if args.at
        else dt.datetime.now().astimezone()
    )
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        parser.error("--at must include a UTC offset")
    if abs((dt.datetime.now().astimezone() - generated_at).total_seconds()) > 900:
        parser.error(
            "live fetch --at must be within 15 minutes of wall-clock time; "
            "historical point-in-time rebuilds must consume previously sealed pages"
        )
    years = sorted({int(value.strip()) for value in args.years.split(",")})
    latest_balance_date = args.latest_balance_date or _default_latest_balance_date(
        generated_at.date()
    )
    if len(years) < 2:
        parser.error("--years must contain at least two annual periods")
    if args.page_size <= 0 or args.page_size > 500:
        parser.error("--page-size must be between 1 and 500")

    companies_path = Path(args.companies)
    companies = read_jsonl(companies_path)
    secucodes = {_secucode(item) for item in companies}
    raw_root = Path(args.raw_dir) / generated_at.date().isoformat()
    raw_root.mkdir(parents=True, exist_ok=True)

    records: dict[str, dict[int, list[dict[str, Any]]]] = {
        "income": {},
        "balance": {},
    }
    input_artifacts: dict[str, dict[str, Any]] = {}
    for kind, report in REPORTS.items():
        for year in years:
            rows = _fetch_report(
                endpoint=args.endpoint,
                report_name=report["name"],
                columns=report["columns"],
                report_date=f"{year}-12-31",
                page_size=args.page_size,
                secucodes=secucodes,
            )
            payload = {
                "schema_version": 1,
                "source": args.endpoint,
                "report_name": report["name"],
                "report_date": f"{year}-12-31",
                "fetched_at": generated_at.isoformat(),
                "record_count": len(rows),
                "records": rows,
            }
            path = raw_root / f"{kind}-{year}.json"
            sealed = seal_json(
                path,
                payload,
                artifact_type=f"magic_formula_raw_{kind}",
                sealed_at=generated_at,
            )
            records[kind][year] = rows
            input_artifacts[f"{kind}_{year}"] = {
                "path": path.as_posix(),
                "sha256": sealed.sha256,
                "record_count": len(rows),
            }

    latest_balance_rows = _fetch_report(
        endpoint=args.endpoint,
        report_name=REPORTS["balance"]["name"],
        columns=REPORTS["balance"]["columns"],
        report_date=latest_balance_date,
        page_size=args.page_size,
        secucodes=secucodes,
    )
    latest_balance_payload = {
        "schema_version": 1,
        "source": args.endpoint,
        "report_name": REPORTS["balance"]["name"],
        "report_date": latest_balance_date,
        "fetched_at": generated_at.isoformat(),
        "record_count": len(latest_balance_rows),
        "records": latest_balance_rows,
    }
    latest_balance_path = raw_root / "balance-latest.json"
    latest_balance_sealed = seal_json(
        latest_balance_path,
        latest_balance_payload,
        artifact_type="magic_formula_source_balance",
        sealed_at=generated_at,
    )
    input_artifacts["balance_latest"] = {
        "path": latest_balance_path.as_posix(),
        "sha256": latest_balance_sealed.sha256,
        "record_count": len(latest_balance_rows),
    }

    snapshot = build_magic_formula_snapshot(
        companies=companies,
        income_records_by_year=records["income"],
        balance_records_by_year=records["balance"],
        latest_balance_records=latest_balance_rows,
        latest_balance_date=latest_balance_date,
        generated_at=generated_at,
        market_snapshot_sha256=_sha256(companies_path),
        source=args.endpoint,
    )
    snapshot["inputs"] = {
        "market_snapshot_path": companies_path.as_posix(),
        "market_snapshot_sha256": _sha256(companies_path),
        "raw_artifacts": input_artifacts,
    }
    output = Path(args.output)
    sealed = seal_json(
        output,
        snapshot,
        artifact_type="magic_formula_snapshot",
        sealed_at=generated_at,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "path": output.as_posix(),
                "sha256": sealed.sha256,
                "item_count": snapshot["item_count"],
                "eligible_count": snapshot["eligible_count"],
                "excluded_count": snapshot["excluded_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _fetch_report(
    *,
    endpoint: str,
    report_name: str,
    columns: list[str],
    report_date: str,
    page_size: int,
    secucodes: set[str],
) -> list[dict[str, Any]]:
    first = _request_page(
        endpoint=endpoint,
        report_name=report_name,
        columns=columns,
        report_date=report_date,
        page_size=page_size,
        page_number=1,
    )
    pages = int(first["pages"])
    all_rows = list(first["data"])
    for page in range(2, pages + 1):
        response = _request_page(
            endpoint=endpoint,
            report_name=report_name,
            columns=columns,
            report_date=report_date,
            page_size=page_size,
            page_number=page,
        )
        all_rows.extend(response["data"])
    filtered = [
        dict(row)
        for row in all_rows
        if str(row.get("SECUCODE") or "") in secucodes
    ]
    return sorted(
        filtered,
        key=lambda row: (
            str(row.get("SECURITY_CODE") or ""),
            str(row.get("UPDATE_DATE") or ""),
            str(row.get("NOTICE_DATE") or ""),
        ),
    )


def _request_page(
    *,
    endpoint: str,
    report_name: str,
    columns: list[str],
    report_date: str,
    page_size: int,
    page_number: int,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "reportName": report_name,
            "columns": ",".join(columns),
            "filter": (
                f"(REPORT_DATE='{report_date}')"
                "(SECURITY_TYPE_CODE='058001001')"
            ),
            "pageNumber": page_number,
            "pageSize": page_size,
            "sortTypes": "1,1",
            "sortColumns": "SECURITY_CODE,UPDATE_DATE",
        }
    )
    url = f"{endpoint}?{query}"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Trading-OS research snapshot/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("success") or not isinstance(payload.get("result"), dict):
                raise RuntimeError(payload.get("message") or "public API returned failure")
            result = payload["result"]
            if not isinstance(result.get("data"), list):
                raise RuntimeError("public API result.data is not an array")
            return {"pages": result.get("pages") or 1, "data": result["data"]}
        except Exception as exc:  # noqa: BLE001 - command-line retry boundary
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {report_name} page {page_number}: {last_error}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secucode(company: dict[str, Any]) -> str:
    ticker = str(company.get("ticker") or str(company["symbol"]).split(":", 1)[-1])
    suffix_by_exchange = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
    exchange = str(company.get("exchange") or "")
    try:
        suffix = suffix_by_exchange[exchange]
    except KeyError as exc:
        raise ValueError(f"unsupported exchange for {ticker}: {exchange}") from exc
    return f"{ticker}.{suffix}"


def _default_latest_balance_date(as_of: dt.date) -> str:
    if as_of.month <= 4:
        return f"{as_of.year - 1}-09-30"
    if as_of.month <= 7:
        return f"{as_of.year}-03-31"
    if as_of.month <= 10:
        return f"{as_of.year}-06-30"
    return f"{as_of.year}-09-30"


if __name__ == "__main__":
    raise SystemExit(main())
