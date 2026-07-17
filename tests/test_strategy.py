from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from stockapp.db import database
import pandas as pd

from stockapp.strategy import MIN_STOCK_BARS, candidate_metrics, planned_position, run_strategy


def weekday_dates(count: int) -> list[str]:
    result = []
    current = date(2025, 1, 1)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


class StrategyTest(unittest.TestCase):
    def test_empty_security_history_is_skipped(self) -> None:
        self.assertIsNone(candidate_metrics(pd.DataFrame(), 0.01))

    def test_minimum_stock_history_is_120_bars(self) -> None:
        rows = []
        for index, trade_date in enumerate(weekday_dates(MIN_STOCK_BARS)):
            close = 20 + index * 0.02 + (0.08 if index % 2 else -0.08)
            rows.append(
                {
                    "trade_date": trade_date,
                    "open": close - 0.05,
                    "high": close + 0.15,
                    "low": close - 0.15,
                    "close": close,
                    "volume": 10_000_000 + (index % 3) * 100_000,
                    "amount": 600_000_000,
                    "trade_status": 1,
                    "is_st": 0,
                }
            )
        frame = pd.DataFrame(rows)
        self.assertIsNone(candidate_metrics(frame.iloc[:-1], 0.01))
        self.assertIsNotNone(candidate_metrics(frame, 0.01))

    def test_position_sizing_respects_risk_and_lot_size(self) -> None:
        quantity, stop = planned_position(50_000, 20, 0.5)
        self.assertEqual(quantity, 200)
        self.assertEqual(stop, 19)

    def test_generates_factor_and_risk_rankings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            with database(path) as connection:
                connection.executemany(
                    "INSERT INTO securities(code, name, market, kind, is_hs300) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("sh.000300", "沪深300", "sh", "index", 0),
                        ("sz.000001", "测试股份", "sz", "stock", 1),
                    ],
                )
                dates = weekday_dates(300)
                rows = []
                for index, trade_date in enumerate(dates):
                    benchmark = 100 + index * 0.08
                    stock = 20 + index * 0.025
                    volume = 10_000_000
                    if index == len(dates) - 1:
                        stock *= 1.06
                        volume *= 2
                    rows.extend(
                        [
                            ("sh.000300", trade_date, benchmark, benchmark + .4, benchmark - .4, benchmark, volume, 1_000_000_000, None, None, None, None, None, 1, 0),
                            ("sz.000001", trade_date, stock - .1, stock + .2, stock - .2, stock, volume, 700_000_000, 1.5, 15, 2, 2, 10, 1, 0),
                        ]
                    )
                rows.append(
                    (
                        "sz.000001", "2026-12-31", stock, stock + .2, stock - .2,
                        stock, volume, 700_000_000, 1.5, 15, 2, 2, 10, 1, 0,
                    )
                )
                connection.executemany(
                    """
                    INSERT INTO daily_bars(
                      code, trade_date, open, high, low, close, volume, amount,
                      turnover_rate, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm,
                      trade_status, is_st
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                with patch("stockapp.strategy.kronos_available", return_value=False):
                    run_id = run_strategy(connection, use_kronos=True)
                run = connection.execute(
                    "SELECT * FROM recommendation_runs WHERE id=?", (run_id,)
                ).fetchone()
                recommendation = connection.execute(
                    "SELECT * FROM recommendations WHERE run_id=? AND code='sz.000001'", (run_id,)
                ).fetchone()
                self.assertEqual(run["market_regime"], "RISK_ON")
                self.assertIn(recommendation["action"], {"BUY", "WATCH"})
                factor = connection.execute(
                    "SELECT * FROM factor_snapshots WHERE run_id=? AND code='sz.000001'",
                    (run_id,),
                ).fetchone()
                risk = connection.execute(
                    "SELECT * FROM risk_alerts WHERE run_id=? AND code='sz.000001'",
                    (run_id,),
                ).fetchone()
                self.assertIsNotNone(factor)
                self.assertGreaterEqual(factor["confidence"], 70)
                self.assertIsNotNone(risk)
                self.assertFalse(json.loads(run["metrics_json"])["kronos"]["used"])


if __name__ == "__main__":
    unittest.main()
