from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from stockapp import create_app
from stockapp.db import database
from stockapp.portfolio import calculate_portfolio


class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        self.app = create_app({"TESTING": True, "DATABASE": str(self.db_path)})
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def seed_stock_scores(self) -> None:
        groups = json.dumps(
            {
                "trend_score": 80,
                "momentum_score": 70,
                "volume_score": 60,
                "volatility_score": 75,
                "valuation_score": 55,
                "quality_score": 65,
            }
        )
        with database(self.db_path) as connection:
            connection.executemany(
                """
                INSERT INTO securities(code, name, market, kind, is_hs300, industry)
                VALUES (?, ?, 'sz', 'stock', 1, ?)
                """,
                [
                    ("sz.000001", "低风险股", "银行"),
                    ("sz.000002", "高风险股", "地产"),
                ],
            )
            run_id = connection.execute(
                """
                INSERT INTO recommendation_runs(
                  trade_date, status, market_regime, message, metrics_json
                ) VALUES ('2026-07-17', 'COMPLETE', 'RISK_ON', '测试', '{}')
                """
            ).lastrowid
            connection.executemany(
                """
                INSERT INTO factor_snapshots(
                  run_id, code, opportunity_score, risk_score, confidence,
                  group_scores_json, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (run_id, "sz.000001", 82, 20, 90, groups, json.dumps({"price": 12.2, "return20": 0.08})),
                    (run_id, "sz.000002", 55, 88, 75, groups, json.dumps({"price": 8.4, "return20": -0.12})),
                ],
            )
            for code, base in (("sz.000001", 12.0), ("sz.000002", 8.0)):
                connection.executemany(
                    """
                    INSERT INTO daily_bars(
                      code, trade_date, open, high, low, close, volume, amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (code, "2026-07-16", base, base + .3, base - .2, base + .1, 1_000_000, 10_000_000),
                        (code, "2026-07-17", base + .1, base + .5, base, base + .2, 1_200_000, 12_000_000),
                    ],
                )
            connection.commit()

    def test_dashboard_and_transaction_lifecycle(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.post(
            "/transactions",
            data={
                "trade_date": "2026-07-16",
                "code": "000001.SZ",
                "side": "BUY",
                "quantity": "500",
                "price": "10.50",
                "fee": "5",
                "stop_price": "9.95",
            },
            follow_redirects=True,
        )
        self.assertIn("000001", response.get_data(as_text=True))
        with database(self.db_path) as connection:
            portfolio = calculate_portfolio(connection)
        self.assertEqual(portfolio["positions"][0]["quantity"], 500)
        self.assertAlmostEqual(portfolio["cash"], 44745.0)

    def test_rejects_invalid_lot_and_oversell(self) -> None:
        response = self.client.post(
            "/transactions",
            data={
                "trade_date": "2026-07-16",
                "code": "600519",
                "side": "BUY",
                "quantity": "50",
                "price": "50",
                "fee": "5",
            },
            follow_redirects=True,
        )
        self.assertIn("multiple of 100", response.get_data(as_text=True))

    def test_dashboard_accepts_legacy_run_without_market_score(self) -> None:
        with database(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO recommendation_runs(
                  trade_date, status, market_regime, message, metrics_json
                ) VALUES (?, 'COMPLETE', 'RISK_OFF', '旧格式结果', ?)
                """,
                (
                    "2026-07-16",
                    json.dumps({"market": {"regime": "RISK_OFF"}}),
                ),
            )
            connection.commit()

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("旧格式结果", response.get_data(as_text=True))

    def test_dashboard_shows_global_overlay_for_running_job(self) -> None:
        with database(self.db_path) as connection:
            connection.execute(
                "INSERT INTO job_runs(job_type, status) VALUES ('SYNC', 'RUNNING')"
            )
            connection.commit()

        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("正在更新日线数据", body)
        self.assertNotIn('data-job-overlay role="status" aria-live="polite" hidden', body)

    def test_dashboard_hides_kronos_when_unavailable(self) -> None:
        self.app.config["KRONOS_AVAILABLE"] = False
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("使用 Kronos", response.get_data(as_text=True))

    def test_stock_pool_search_and_risk_sort(self) -> None:
        self.seed_stock_scores()
        response = self.client.get("/stocks?sort=risk_desc")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertLess(body.index("高风险股"), body.index("低风险股"))

        response = self.client.get("/stocks?q=000001")
        body = response.get_data(as_text=True)
        self.assertIn("低风险股", body)
        self.assertNotIn("高风险股", body)

    def test_stock_detail_contains_chart_data_and_ma_controls(self) -> None:
        self.seed_stock_scores()
        response = self.client.get("/stocks/sz.000001")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("低风险股", body)
        self.assertIn('id="stock-chart-data"', body)
        self.assertIn("2026-07-17", body)
        self.assertIn("MA5", body)
        self.assertIn("MA120", body)
        self.assertIn("反映股价当前是否处于持续、明确的上升方向。", body)
        self.assertIn("反映公司的盈利、增长、现金流和负债状况是否健康。", body)

    def test_stock_detail_contains_complete_price_history(self) -> None:
        self.seed_stock_scores()
        first_date = date(2020, 1, 1)
        with database(self.db_path) as connection:
            connection.executemany(
                """
                INSERT INTO daily_bars(
                  code, trade_date, open, high, low, close, volume, amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "sz.000001",
                        (first_date + timedelta(days=index)).isoformat(),
                        10,
                        10.5,
                        9.5,
                        10.2,
                        1_000_000,
                        10_000_000,
                    )
                    for index in range(361)
                ],
            )
            connection.commit()

        response = self.client.get("/stocks/sz.000001")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"time": "2020-01-01"', body)
        self.assertIn('"time": "2026-07-17"', body)

        chart_script = (
            Path(__file__).parents[1] / "stockapp" / "static" / "stock-chart.js"
        ).read_text()
        self.assertIn("setVisibleLogicalRange", chart_script)
        self.assertIn("Math.min(100, bars.length)", chart_script)
        self.assertNotIn("chart.timeScale().fitContent()", chart_script)

    def test_watchlist_add_filter_and_remove(self) -> None:
        self.seed_stock_scores()
        response = self.client.post(
            "/watchlist/sz.000001",
            data={"action": "add", "return_to": "/stocks?watched=1"},
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("低风险股", body)
        self.assertNotIn("高风险股", body)
        self.assertIn("取消关注 低风险股", body)
        with database(self.db_path) as connection:
            watched = connection.execute(
                "SELECT code FROM watchlist"
            ).fetchall()
        self.assertEqual([row["code"] for row in watched], ["sz.000001"])

        response = self.client.post(
            "/watchlist/sz.000001",
            data={"action": "remove", "return_to": "/stocks/sz.000001"},
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertIn("☆ 加关注", body)
        with database(self.db_path) as connection:
            count = connection.execute("SELECT COUNT(*) count FROM watchlist").fetchone()
        self.assertEqual(count["count"], 0)


if __name__ == "__main__":
    unittest.main()
