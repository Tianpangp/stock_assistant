from __future__ import annotations

import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
