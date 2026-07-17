from __future__ import annotations

import argparse
import json
from dataclasses import asdict

import pandas as pd

from .db import database, finish_job, start_job
from .market_data import (
    backfill_extended_bars,
    backfill_membership_snapshots,
    sync_financial_snapshots,
    sync_market_data,
)
from .strategy import run_strategy


def sync_command(args: argparse.Namespace) -> None:
    with database() as connection:
        job_id = start_job(connection, "SYNC")
        try:
            summary = sync_market_data(
                connection, end_date=args.end, codes=args.codes, limit=args.limit
            )
            payload = asdict(summary)
            finish_job(connection, job_id, "COMPLETE" if not summary.errors else "PARTIAL", payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            finish_job(connection, job_id, "FAILED", str(exc))
            raise


def recommend_command(args: argparse.Namespace) -> None:
    with database() as connection:
        job_id = start_job(connection, "RECOMMEND")
        try:
            run_id = run_strategy(connection, use_kronos=args.kronos, top_k=args.top)
            run = connection.execute(
                "SELECT metrics_json FROM recommendation_runs WHERE id=?", (run_id,)
            ).fetchone()
            model_status = json.loads(run["metrics_json"]).get("kronos", {}) if run else {}
            payload = {
                "run_id": run_id,
                "kronos_requested": bool(model_status.get("requested")),
                "kronos_used": bool(model_status.get("used")),
            }
            finish_job(connection, job_id, "COMPLETE", payload)
            print(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            finish_job(connection, job_id, "FAILED", str(exc))
            raise


def backfill_command(args: argparse.Namespace) -> None:
    with database() as connection:
        summary = backfill_extended_bars(
            connection, start_date=args.start, end_date=args.end, limit=args.limit
        )
        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))


def financial_command(args: argparse.Namespace) -> None:
    with database() as connection:
        summary = sync_financial_snapshots(
            connection, year=args.year, quarter=args.quarter, limit=args.limit
        )
        print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))


def membership_command(args: argparse.Namespace) -> None:
    dates = pd.date_range(args.start, args.end, freq="ME").strftime("%Y-%m-%d").tolist()
    if args.end not in dates:
        dates.append(pd.Timestamp(args.end).date().isoformat())
    with database() as connection:
        summary = backfill_membership_snapshots(connection, dates)
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share daily decision-support system")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync", help="Incrementally update HS300 daily bars")
    sync.add_argument("--end", help="End date in YYYY-MM-DD format")
    sync.add_argument("--codes", nargs="*", help="Only synchronize these BaoStock codes")
    sync.add_argument("--limit", type=int, help="Limit stocks for a smoke test")
    sync.set_defaults(func=sync_command)

    recommend = subparsers.add_parser("recommend", help="Generate today's report")
    recommend.add_argument("--kronos", action="store_true", help="Score shortlisted stocks with Kronos")
    recommend.add_argument("--top", type=int, default=5)
    recommend.set_defaults(func=recommend_command)

    backfill = subparsers.add_parser(
        "backfill-bars", help="Backfill turnover and daily valuation fields"
    )
    backfill.add_argument("--start", default="2018-01-01")
    backfill.add_argument("--end")
    backfill.add_argument("--limit", type=int)
    backfill.set_defaults(func=backfill_command)

    financial = subparsers.add_parser(
        "sync-financials", help="Cache structured quarterly financial metrics"
    )
    financial.add_argument("--year", type=int, required=True)
    financial.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], required=True)
    financial.add_argument("--limit", type=int)
    financial.set_defaults(func=financial_command)

    membership = subparsers.add_parser(
        "backfill-membership", help="Cache monthly historical HS300 membership snapshots"
    )
    membership.add_argument("--start", default="2018-01-01")
    membership.add_argument("--end", default=pd.Timestamp.today().date().isoformat())
    membership.set_defaults(func=membership_command)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
