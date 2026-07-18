from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from .config import DATABASE_PATH, kronos_available
from .db import database, finish_job, get_setting, set_setting, start_job
from .market_data import sync_market_data
from .portfolio import calculate_portfolio, record_transaction
from .strategy import run_strategy


JOB_LOCK = threading.Lock()

ACTION_LABELS = {
    "BUY": "买入",
    "WATCH": "关注",
    "HOLD": "持有",
    "REDUCE": "减仓",
    "SELL": "卖出",
}
MARKET_LABELS = {
    "RISK_ON": "允许开仓",
    "CAUTIOUS": "谨慎观望",
    "RISK_OFF": "暂停开仓",
}
JOB_TYPE_LABELS = {"SYNC": "行情更新", "RECOMMEND": "策略计算"}
JOB_STATUS_LABELS = {"RUNNING": "运行中", "COMPLETE": "已完成", "FAILED": "失败"}
RISK_LABELS = {"HIGH_RISK": "高风险", "CAUTION": "需谨慎"}
STOCK_SORTS = {
    "opportunity_desc": ("机会分：高到低", "f.opportunity_score DESC"),
    "opportunity_asc": ("机会分：低到高", "f.opportunity_score ASC"),
    "risk_desc": ("风险分：高到低", "f.risk_score DESC"),
    "risk_asc": ("风险分：低到高", "f.risk_score ASC"),
    "confidence_desc": ("置信度：高到低", "f.confidence DESC"),
    "confidence_asc": ("置信度：低到高", "f.confidence ASC"),
}
FACTOR_GROUP_LABELS = (
    ("trend_score", "趋势"),
    ("momentum_score", "动量"),
    ("volume_score", "量价"),
    ("volatility_score", "波动质量"),
    ("valuation_score", "估值"),
    ("quality_score", "财务质量"),
)


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

    try:
        with database(db_path) as connection:
            job_id = start_job(connection, job_type)
    except Exception:
        JOB_LOCK.release()
        raise

    def worker() -> None:
        try:
            with database(db_path) as connection:
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
        KRONOS_AVAILABLE=kronos_available(),
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

    @app.template_filter("action_label")
    def action_label(value: object) -> str:
        return ACTION_LABELS.get(str(value), str(value))

    @app.template_filter("market_label")
    def market_label(value: object) -> str:
        return MARKET_LABELS.get(str(value), str(value))

    @app.template_filter("job_type_label")
    def job_type_label(value: object) -> str:
        return JOB_TYPE_LABELS.get(str(value), str(value))

    @app.template_filter("job_status_label")
    def job_status_label(value: object) -> str:
        return JOB_STATUS_LABELS.get(str(value), str(value))

    @app.template_filter("risk_label")
    def risk_label(value: object) -> str:
        return RISK_LABELS.get(str(value), str(value))

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
            kronos_available=app.config["KRONOS_AVAILABLE"],
        )

    @app.get("/stocks")
    def stock_list():
        query = request.args.get("q", "").strip()
        watched_only = request.args.get("watched") == "1"
        sort_key = request.args.get("sort", "opportunity_desc")
        if sort_key not in STOCK_SORTS:
            sort_key = "opportunity_desc"
        with database(db_path) as connection:
            factor_run = connection.execute(
                """
                SELECT r.id, r.trade_date, r.generated_at
                FROM recommendation_runs r
                WHERE EXISTS (SELECT 1 FROM factor_snapshots f WHERE f.run_id=r.id)
                ORDER BY r.id DESC LIMIT 1
                """
            ).fetchone()
            stocks = []
            if factor_run:
                params: list[object] = [factor_run["id"]]
                conditions = []
                if query:
                    conditions.append("(s.name LIKE ? OR s.code LIKE ?)")
                    pattern = f"%{query}%"
                    params.extend([pattern, pattern])
                if watched_only:
                    conditions.append("w.code IS NOT NULL")
                where = "" if not conditions else "AND " + " AND ".join(conditions)
                rows = connection.execute(
                    f"""
                    SELECT f.code, s.name, s.industry, f.opportunity_score,
                           f.risk_score, f.confidence, f.metrics_json,
                           CASE WHEN w.code IS NULL THEN 0 ELSE 1 END AS is_watched
                    FROM factor_snapshots f
                    JOIN securities s ON s.code=f.code
                    LEFT JOIN watchlist w ON w.code=f.code
                    WHERE f.run_id=? AND s.is_hs300=1 {where}
                    ORDER BY {STOCK_SORTS[sort_key][1]}, s.code
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    metrics = parse_json(row["metrics_json"], {})
                    stocks.append(
                        {
                            **dict(row),
                            "price": metrics.get("price") if isinstance(metrics, dict) else None,
                            "return20": metrics.get("return20") if isinstance(metrics, dict) else None,
                        }
                    )
        return render_template(
            "stocks.html",
            stocks=stocks,
            query=query,
            watched_only=watched_only,
            sort_key=sort_key,
            sort_options=STOCK_SORTS,
            factor_run=factor_run,
        )

    @app.get("/stocks/<path:code>")
    def stock_detail(code: str):
        try:
            normalized = normalize_code(code)
        except ValueError:
            abort(404)
        with database(db_path) as connection:
            security = connection.execute(
                "SELECT * FROM securities WHERE code=?", (normalized,)
            ).fetchone()
            if not security:
                abort(404)
            factor = connection.execute(
                """
                SELECT f.*, r.trade_date AS score_date
                FROM factor_snapshots f
                JOIN recommendation_runs r ON r.id=f.run_id
                WHERE f.code=? ORDER BY f.run_id DESC LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            bars = connection.execute(
                """
                SELECT trade_date, open, high, low, close, volume, amount
                FROM daily_bars WHERE code=?
                ORDER BY trade_date
                """,
                (normalized,),
            ).fetchall()
            recommendation = connection.execute(
                """
                SELECT rec.*, r.trade_date AS recommendation_date
                FROM recommendations rec
                JOIN recommendation_runs r ON r.id=rec.run_id
                WHERE rec.code=? ORDER BY rec.run_id DESC LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            is_watched = connection.execute(
                "SELECT 1 FROM watchlist WHERE code=?", (normalized,)
            ).fetchone() is not None
        chart_data = [
            {
                "time": row["trade_date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            for row in bars
        ]
        factor_data = None
        if factor:
            factor_data = {
                **dict(factor),
                "groups": parse_json(factor["group_scores_json"], {}),
                "metrics": parse_json(factor["metrics_json"], {}),
            }
        recommendation_data = None
        if recommendation:
            recommendation_data = {
                **dict(recommendation),
                "reasons": parse_json(recommendation["reasons_json"], []),
            }
        latest = chart_data[-1] if chart_data else None
        previous = chart_data[-2] if len(chart_data) > 1 else None
        daily_change = (
            latest["close"] / previous["close"] - 1 if latest and previous else None
        )
        return render_template(
            "stock_detail.html",
            security=security,
            factor=factor_data,
            recommendation=recommendation_data,
            is_watched=is_watched,
            chart_data=chart_data,
            latest=latest,
            daily_change=daily_change,
            factor_group_labels=FACTOR_GROUP_LABELS,
        )

    @app.post("/watchlist/<path:code>")
    def update_watchlist(code: str):
        try:
            normalized = normalize_code(code)
        except ValueError:
            abort(404)
        action = request.form.get("action", "add")
        with database(db_path) as connection:
            security = connection.execute(
                "SELECT name FROM securities WHERE code=?", (normalized,)
            ).fetchone()
            if not security:
                abort(404)
            if action == "remove":
                connection.execute("DELETE FROM watchlist WHERE code=?", (normalized,))
                message = f"已取消关注 {security['name']}。"
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO watchlist(code) VALUES (?)", (normalized,)
                )
                message = f"已关注 {security['name']}。"
        flash(message, "success")
        return_to = request.form.get("return_to", "")
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = url_for("stock_detail", code=normalized)
        return redirect(return_to)

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
