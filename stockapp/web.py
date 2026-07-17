from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from flask import Flask, flash, redirect, render_template, request, url_for

from .config import DATABASE_PATH
from .db import database, finish_job, get_setting, set_setting, start_job
from .market_data import sync_market_data
from .portfolio import calculate_portfolio, record_transaction
from .strategy import run_strategy


JOB_LOCK = threading.Lock()


def normalize_code(value: str) -> str:
    raw = value.strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", raw)
    if match:
        return f"{match.group(2).lower()}.{match.group(1)}"
    if re.fullmatch(r"(sh|sz|bj)\.\d{6}", value.strip(), re.I):
        return value.strip().lower()
    if re.fullmatch(r"\d{6}", raw):
        prefix = "sh" if raw.startswith(("5", "6", "9")) else "bj" if raw.startswith(("4", "8")) else "sz"
        return f"{prefix}.{raw}"
    raise ValueError("股票代码格式应为 600519.SH、sh.600519 或 600519")


def parse_json(value: str, fallback: object) -> object:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def launch_background(db_path: Path, job_type: str, callback: Callable) -> bool:
    if not JOB_LOCK.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            with database(db_path) as connection:
                job_id = start_job(connection, job_type)
                try:
                    result = callback(connection)
                    message = result if isinstance(result, dict) else {"result": str(result)}
                    finish_job(connection, job_id, "COMPLETE", message)
                except Exception as exc:
                    finish_job(connection, job_id, "FAILED", str(exc))
        finally:
            JOB_LOCK.release()

    threading.Thread(target=worker, name=f"stock-{job_type.lower()}", daemon=True).start()
    return True


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        SECRET_KEY="local-stock-assistant",
        DATABASE=str(DATABASE_PATH),
    )
    if test_config:
        app.config.update(test_config)
    db_path = Path(app.config["DATABASE"])
    with database(db_path):
        pass

    @app.template_filter("money")
    def money(value: object) -> str:
        return f"¥{float(value or 0):,.2f}"

    @app.template_filter("pct")
    def pct(value: object) -> str:
        return f"{float(value or 0):+.2%}"

    @app.get("/")
    def dashboard():
        with database(db_path) as connection:
            portfolio = calculate_portfolio(connection)
            latest_run = connection.execute(
                "SELECT * FROM recommendation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            recommendations = []
            risk_alerts = []
            run_metrics = {}
            market_score = None
            if latest_run:
                run_metrics = parse_json(latest_run["metrics_json"], {})
                if not isinstance(run_metrics, dict):
                    run_metrics = {}
                market_metrics = run_metrics.get("market", {})
                if isinstance(market_metrics, dict):
                    market_score = market_metrics.get("market_score")
                rows = connection.execute(
                    """
                    SELECT r.*, s.name FROM recommendations r
                    JOIN securities s ON s.code=r.code
                    WHERE r.run_id=? ORDER BY
                      CASE r.action WHEN 'SELL' THEN 1 WHEN 'REDUCE' THEN 2 WHEN 'BUY' THEN 3 WHEN 'HOLD' THEN 4 ELSE 5 END,
                      r.rank
                    """,
                    (latest_run["id"],),
                ).fetchall()
                recommendations = [
                    {
                        **dict(row),
                        "reasons": parse_json(row["reasons_json"], []),
                        "metrics": parse_json(row["metrics_json"], {}),
                    }
                    for row in rows
                ]
                risk_rows = connection.execute(
                    """
                    SELECT r.*, s.name FROM risk_alerts r
                    JOIN securities s ON s.code=r.code
                    WHERE r.run_id=? ORDER BY r.rank
                    """,
                    (latest_run["id"],),
                ).fetchall()
                risk_alerts = [
                    {
                        **dict(row),
                        "reasons": parse_json(row["reasons_json"], []),
                        "metrics": parse_json(row["metrics_json"], {}),
                    }
                    for row in risk_rows
                ]
            transactions = connection.execute(
                """
                SELECT t.*, s.name FROM transactions t JOIN securities s ON s.code=t.code
                ORDER BY trade_date DESC, id DESC LIMIT 30
                """
            ).fetchall()
            latest_job = connection.execute(
                "SELECT * FROM job_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            data_status = connection.execute(
                """
                SELECT MAX(trade_date) latest_date, COUNT(DISTINCT code) securities,
                       COUNT(*) bars FROM daily_bars
                """
            ).fetchone()
            initial_capital = get_setting(connection, "initial_capital", "50000")
        return render_template(
            "dashboard.html",
            today=date.today().isoformat(),
            portfolio=portfolio,
            latest_run=latest_run,
            recommendations=recommendations,
            risk_alerts=risk_alerts,
            run_metrics=run_metrics,
            market_score=market_score,
            transactions=transactions,
            latest_job=latest_job,
            data_status=data_status,
            initial_capital=initial_capital,
        )

    @app.post("/transactions")
    def add_transaction():
        try:
            code = normalize_code(request.form.get("code", ""))
            stop_raw = request.form.get("stop_price", "").strip()
            with database(db_path) as connection:
                record_transaction(
                    connection,
                    trade_date=request.form.get("trade_date") or date.today().isoformat(),
                    code=code,
                    side=request.form.get("side", ""),
                    quantity=int(request.form.get("quantity", "0")),
                    price=float(request.form.get("price", "0")),
                    fee=float(request.form.get("fee", "0") or 0),
                    stop_price=float(stop_raw) if stop_raw else None,
                    notes=request.form.get("notes", ""),
                    name=request.form.get("name", "").strip() or None,
                )
            flash("成交已记录。", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/settings")
    def update_settings():
        try:
            capital = float(request.form.get("initial_capital", "0"))
            if capital <= 0:
                raise ValueError("初始资金必须大于0")
            with database(db_path) as connection:
                if connection.execute("SELECT COUNT(*) count FROM transactions").fetchone()["count"]:
                    raise ValueError("已有成交后不能直接修改初始资金")
                set_setting(connection, "initial_capital", capital)
            flash("账户设置已更新。", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/jobs/sync")
    def start_sync():
        started = launch_background(
            db_path,
            "SYNC",
            lambda connection: sync_market_data(connection).__dict__,
        )
        flash("行情增量同步已启动。" if started else "已有任务正在运行。", "success" if started else "error")
        return redirect(url_for("dashboard"))

    @app.post("/jobs/recommend")
    def start_recommend():
        use_kronos = request.form.get("use_kronos") == "1"
        started = launch_background(
            db_path,
            "RECOMMEND",
            lambda connection: {"run_id": run_strategy(connection, use_kronos=use_kronos)},
        )
        flash("选股任务已启动。" if started else "已有任务正在运行。", "success" if started else "error")
        return redirect(url_for("dashboard"))

    return app
