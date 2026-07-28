from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import baostock as bs
import pandas as pd

from .config import INITIAL_HISTORY_DATE


BAR_FIELDS = (
    "date,open,high,low,close,volume,amount,turn,peTTM,pbMRQ,psTTM,"
    "pcfNcfTTM,tradestatus,isST"
)
INDEX_CODE = "sh.000300"


def result_frame(result: object) -> pd.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    rows: list[list[str]] = []
    while result.error_code == "0" and result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


class BaoStockSession:
    def __enter__(self) -> "BaoStockSession":
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        bs.logout()

    def hs300(self, as_of: str = "") -> pd.DataFrame:
        return result_frame(bs.query_hs300_stocks(date=as_of))

    def industries(self, as_of: str = "") -> pd.DataFrame:
        return result_frame(bs.query_stock_industry(date=as_of))

    def stock_basic(self, code: str) -> pd.DataFrame:
        return result_frame(bs.query_stock_basic(code=code))

    def bars(self, code: str, start: str, end: str) -> pd.DataFrame:
        result = bs.query_history_k_data_plus(
            code,
            BAR_FIELDS,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2" if code != INDEX_CODE else "3",
        )
        return result_frame(result)

    def financial(self, data_type: str, code: str, year: int, quarter: int) -> pd.DataFrame:
        methods = {
            "profit": bs.query_profit_data,
            "growth": bs.query_growth_data,
            "operation": bs.query_operation_data,
            "balance": bs.query_balance_data,
            "cash_flow": bs.query_cash_flow_data,
            "dupont": bs.query_dupont_data,
        }
        if data_type not in methods:
            raise ValueError(f"Unsupported financial data type: {data_type}")
        return result_frame(methods[data_type](code=code, year=year, quarter=quarter))


def normalize_end_date(value: str | None = None) -> str:
    if not value:
        return date.today().isoformat()
    return pd.Timestamp(value).date().isoformat()


def lookup_a_share(code: str) -> dict[str, object] | None:
    if not code.startswith(("sh.", "sz.")):
        return None
    with BaoStockSession() as session:
        frame = session.stock_basic(code)
    if frame.empty:
        return None
    matches = frame[frame["code"] == code]
    if matches.empty:
        return None
    record = matches.iloc[0].to_dict()
    if str(record.get("type") or "") != "1":
        return None
    return {
        "code": code,
        "name": str(record.get("code_name") or code),
        "market": code.split(".", 1)[0],
        "active": int(str(record.get("status") or "1") == "1"),
    }


def sync_universe(connection: sqlite3.Connection, session: BaoStockSession) -> int:
    frame = session.hs300()
    if frame.empty:
        raise RuntimeError("BaoStock returned an empty HS300 constituent list")
    connection.execute("UPDATE securities SET is_hs300=0 WHERE kind='stock'")
    rows = []
    for row in frame.to_dict("records"):
        code = row.get("code", "")
        if not code:
            continue
        rows.append((code, row.get("code_name") or code, code.split(".", 1)[0]))
    connection.executemany(
        """
        INSERT INTO securities(
          code, name, market, kind, is_hs300, is_verified, active, updated_at
        ) VALUES (?, ?, ?, 'stock', 1, 1, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(code) DO UPDATE SET
          name=excluded.name, market=excluded.market, is_hs300=1,
          is_verified=1, active=1, updated_at=CURRENT_TIMESTAMP
        """,
        rows,
    )
    connection.execute(
        """
        INSERT INTO securities(code, name, market, kind, active)
        VALUES (?, '沪深300', 'sh', 'index', 1)
        ON CONFLICT(code) DO UPDATE SET name=excluded.name, active=1
        """,
        (INDEX_CODE,),
    )
    snapshot_date = str(frame["updateDate"].max()) if "updateDate" in frame else date.today().isoformat()
    connection.executemany(
        """
        INSERT OR REPLACE INTO index_membership_snapshots(index_code, snapshot_date, code, name)
        VALUES (?, ?, ?, ?)
        """,
        [(INDEX_CODE, snapshot_date, code, name) for code, name, _ in rows],
    )
    connection.commit()
    return len(rows)


def sync_industries(connection: sqlite3.Connection, session: BaoStockSession) -> int:
    frame = session.industries()
    if frame.empty:
        return 0
    known = {row["code"] for row in connection.execute("SELECT code FROM securities")}
    rows = []
    history = []
    for row in frame.to_dict("records"):
        code = row.get("code")
        industry = row.get("industry")
        if code not in known or not industry:
            continue
        updated = row.get("updateDate") or date.today().isoformat()
        classification = row.get("industryClassification") or ""
        rows.append((industry, classification, updated, code))
        history.append((code, updated, industry, classification))
    connection.executemany(
        """
        UPDATE securities SET industry=?, industry_classification=?, industry_updated_at=?,
          updated_at=CURRENT_TIMESTAMP WHERE code=?
        """,
        rows,
    )
    connection.executemany(
        """
        INSERT OR REPLACE INTO industry_history(code, effective_date, industry, classification)
        VALUES (?, ?, ?, ?)
        """,
        history,
    )
    connection.commit()
    return len(rows)


def latest_bar_date(connection: sqlite3.Connection, code: str) -> str | None:
    row = connection.execute(
        "SELECT MAX(trade_date) AS last_date FROM daily_bars WHERE code=?", (code,)
    ).fetchone()
    return row["last_date"] if row and row["last_date"] else None


def next_date(value: str) -> str:
    return (datetime.strptime(value, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


def upsert_bars(connection: sqlite3.Connection, code: str, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    rows = []
    for row in frame.to_dict("records"):
        try:
            prices = [float(row[key]) for key in ("open", "high", "low", "close")]
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if min(prices) <= 0:
            continue
        def optional_float(key: str) -> float | None:
            value = row.get(key)
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        rows.append(
            (
                code,
                str(row["date"]),
                *prices,
                volume,
                amount,
                optional_float("turn"),
                optional_float("peTTM"),
                optional_float("pbMRQ"),
                optional_float("psTTM"),
                optional_float("pcfNcfTTM"),
                int(row.get("tradestatus") or 1),
                int(row.get("isST") or 0),
            )
        )
    connection.executemany(
        """
        INSERT INTO daily_bars(
          code, trade_date, open, high, low, close, volume, amount,
          turnover_rate, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm, trade_status, is_st
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, trade_date) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low,
          close=excluded.close, volume=excluded.volume, amount=excluded.amount,
          turnover_rate=excluded.turnover_rate, pe_ttm=excluded.pe_ttm,
          pb_mrq=excluded.pb_mrq, ps_ttm=excluded.ps_ttm,
          pcf_ncf_ttm=excluded.pcf_ncf_ttm,
          trade_status=excluded.trade_status, is_st=excluded.is_st
        """,
        rows,
    )
    return len(rows)


@dataclass
class SyncSummary:
    securities: int
    requested: int
    updated: int
    bars: int
    skipped: int
    errors: list[str]


def sync_market_data(
    connection: sqlite3.Connection,
    end_date: str | None = None,
    codes: Iterable[str] | None = None,
    limit: int | None = None,
) -> SyncSummary:
    end = normalize_end_date(end_date)
    with BaoStockSession() as session:
        security_count = sync_universe(connection, session)
        sync_industries(connection, session)
        if codes:
            selected = list(dict.fromkeys(codes))
        else:
            selected = [
                row["code"]
                for row in connection.execute(
                    """
                    SELECT code FROM securities
                    WHERE is_hs300=1 OR is_tracked=1 ORDER BY code
                    """
                )
            ]
        selected = [INDEX_CODE, *[code for code in selected if code != INDEX_CODE]]
        if limit:
            selected = selected[: limit + 1]

        updated = bars = skipped = 0
        errors: list[str] = []
        for code in selected:
            last = latest_bar_date(connection, code)
            start = next_date(last) if last else INITIAL_HISTORY_DATE
            if start > end:
                skipped += 1
                continue
            try:
                frame = session.bars(code, start, end)
                count = upsert_bars(connection, code, frame)
                bars += count
                updated += int(count > 0)
                connection.commit()
            except Exception as exc:
                connection.rollback()
                errors.append(f"{code}: {exc}")

    return SyncSummary(
        securities=security_count,
        requested=len(selected),
        updated=updated,
        bars=bars,
        skipped=skipped,
        errors=errors,
    )


def backfill_extended_bars(
    connection: sqlite3.Connection,
    start_date: str = INITIAL_HISTORY_DATE,
    end_date: str | None = None,
    limit: int | None = None,
) -> SyncSummary:
    end = normalize_end_date(end_date)
    codes = [
        row["code"]
        for row in connection.execute(
            """
            SELECT code FROM securities
            WHERE is_hs300=1 OR is_tracked=1 ORDER BY code
            """
        )
    ]
    if limit:
        codes = codes[:limit]
    updated = bars = skipped = 0
    errors: list[str] = []
    with BaoStockSession() as session:
        sync_industries(connection, session)
        for code in codes:
            coverage = connection.execute(
                """
                SELECT COUNT(*) total, SUM(pb_mrq IS NOT NULL) enriched
                FROM daily_bars WHERE code=?
                """,
                (code,),
            ).fetchone()
            if coverage and coverage["total"] and coverage["enriched"] >= coverage["total"] * 0.95:
                skipped += 1
                continue
            try:
                frame = session.bars(code, start_date, end)
                count = upsert_bars(connection, code, frame)
                bars += count
                updated += int(count > 0)
                connection.commit()
            except Exception as exc:
                connection.rollback()
                errors.append(f"{code}: {exc}")
    return SyncSummary(
        securities=len(codes), requested=len(codes), updated=updated,
        bars=bars, skipped=skipped, errors=errors
    )


@dataclass
class FinancialSyncSummary:
    securities: int
    reports: int
    datasets: int
    errors: list[str]


def sync_financial_snapshots(
    connection: sqlite3.Connection,
    year: int,
    quarter: int,
    limit: int | None = None,
) -> FinancialSyncSummary:
    codes = [
        row["code"]
        for row in connection.execute(
            """
            SELECT code FROM securities
            WHERE is_hs300=1 OR is_tracked=1 ORDER BY code
            """
        )
    ]
    if limit:
        codes = codes[:limit]
    data_types = ("profit", "growth", "operation", "balance", "cash_flow", "dupont")
    reports = datasets = 0
    errors: list[str] = []
    with BaoStockSession() as session:
        for code in codes:
            code_has_report = False
            for data_type in data_types:
                try:
                    frame = session.financial(data_type, code, year, quarter)
                    if frame.empty:
                        continue
                    record = frame.iloc[-1].to_dict()
                    publish_date = str(record.pop("pubDate", "") or "")
                    report_period = str(record.pop("statDate", "") or "")
                    record.pop("code", None)
                    if not publish_date or not report_period:
                        continue
                    metrics = {
                        key: float(value) if value not in (None, "") else None
                        for key, value in record.items()
                    }
                    connection.execute(
                        """
                        INSERT INTO financial_snapshots(
                          code, report_period, publish_date, data_type, metrics_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(code, report_period, data_type) DO UPDATE SET
                          publish_date=excluded.publish_date, metrics_json=excluded.metrics_json,
                          updated_at=CURRENT_TIMESTAMP
                        """,
                        (code, report_period, publish_date, data_type, json.dumps(metrics)),
                    )
                    datasets += 1
                    code_has_report = True
                except Exception as exc:
                    errors.append(f"{code}/{data_type}: {exc}")
            reports += int(code_has_report)
            connection.commit()
    return FinancialSyncSummary(len(codes), reports, datasets, errors)


def backfill_membership_snapshots(
    connection: sqlite3.Connection, dates: Iterable[str]
) -> dict[str, object]:
    snapshots = members = 0
    errors: list[str] = []
    with BaoStockSession() as session:
        for snapshot_date in dates:
            try:
                frame = session.hs300(snapshot_date)
                rows = []
                for row in frame.to_dict("records"):
                    code = row.get("code")
                    name = row.get("code_name") or code
                    if not code:
                        continue
                    market = code.split(".", 1)[0]
                    connection.execute(
                        """
                        INSERT INTO securities(code, name, market) VALUES (?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET name=excluded.name
                        """,
                        (code, name, market),
                    )
                    rows.append((INDEX_CODE, snapshot_date, code, name))
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO index_membership_snapshots(
                      index_code, snapshot_date, code, name
                    ) VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
                connection.commit()
                snapshots += 1
                members += len(rows)
            except Exception as exc:
                connection.rollback()
                errors.append(f"{snapshot_date}: {exc}")
    return {"snapshots": snapshots, "members": members, "errors": errors}
