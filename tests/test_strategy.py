from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stockapp.db import database
import pandas as pd

from stockapp.strategy import candidate_metrics, planned_position, run_strategy


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
                run_id = run_strategy(connection)
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


if __name__ == "__main__":
    unittest.main()
