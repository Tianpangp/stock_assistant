from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
import pandas as pd

from .config import kronos_available
from .db import set_setting
from .market_data import INDEX_CODE
from .portfolio import calculate_portfolio


MIN_STOCK_BARS = 120


def load_bars(connection: sqlite3.Connection, code: str, limit: int = 600) -> pd.DataFrame:
    rows = connection.execute(
        """
        SELECT trade_date, open, high, low, close, volume, amount,
               turnover_rate, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm,
               trade_status, is_st
        FROM daily_bars WHERE code=? ORDER BY trade_date DESC LIMIT ?
        """,
        (code, limit),
    ).fetchall()
    return pd.DataFrame([dict(row) for row in reversed(rows)])


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for window in (20, 60, 120):
        result[f"ma{window}"] = result["close"].rolling(window).mean()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr14"] = true_range.rolling(14).mean()
    result["return5"] = result["close"].pct_change(5)
    result["return20"] = result["close"].pct_change(20)
    result["previous_high20"] = result["close"].shift(1).rolling(20).max()
    result["volume_median20"] = result["volume"].shift(1).rolling(20).median()
    result["amount_mean20"] = result["amount"].rolling(20).mean()
    result["amount_mean5"] = result["amount"].rolling(5).mean()
    result["amount_ratio5_20"] = result["amount_mean5"] / result["amount_mean20"]
    result["ema12"] = result["close"].ewm(span=12, adjust=False).mean()
    result["ema26"] = result["close"].ewm(span=26, adjust=False).mean()
    result["macd"] = result["ema12"] - result["ema26"]
    result["macd_signal"] = result["macd"].ewm(span=9, adjust=False).mean()
    result["macd_hist"] = result["macd"] - result["macd_signal"]

    delta = result["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    relative = gain / loss.replace(0, np.nan)
    result["rsi14"] = (100 - 100 / (1 + relative)).fillna(50)

    low9 = result["low"].rolling(9).min()
    high9 = result["high"].rolling(9).max()
    rsv = (result["close"] - low9) / (high9 - low9).replace(0, np.nan) * 100
    result["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    result["kdj_d"] = result["kdj_k"].ewm(com=2, adjust=False).mean()
    result["kdj_j"] = 3 * result["kdj_k"] - 2 * result["kdj_d"]

    direction = np.sign(delta).fillna(0)
    result["obv"] = (direction * result["volume"]).cumsum()
    result["obv_slope10"] = result["obv"].diff(10) / result["volume"].rolling(20).mean()
    typical_price = (result["high"] + result["low"] + result["close"]) / 3
    money_flow = typical_price * result["volume"]
    positive_flow = money_flow.where(typical_price.diff() > 0, 0).rolling(14).sum()
    negative_flow = money_flow.where(typical_price.diff() < 0, 0).rolling(14).sum()
    money_ratio = positive_flow / negative_flow.replace(0, np.nan)
    result["mfi14"] = (100 - 100 / (1 + money_ratio)).fillna(50)

    daily_return = result["close"].pct_change()
    result["daily_return"] = daily_return
    result["volatility20"] = daily_return.rolling(20).std() * np.sqrt(252)
    result["downside_vol20"] = daily_return.where(daily_return < 0, 0).rolling(20).std() * np.sqrt(252)
    result["drawdown20"] = result["close"] / result["close"].rolling(20).max() - 1
    result["ma20_slope5"] = result["ma20"] / result["ma20"].shift(5) - 1
    up_volume = result["volume"].where(daily_return > 0, 0).rolling(20).sum()
    down_volume = result["volume"].where(daily_return < 0, 0).rolling(20).sum()
    result["up_down_volume_ratio"] = up_volume / down_volume.replace(0, np.nan)
    if "turnover_rate" in result:
        result["turnover_mean20"] = result["turnover_rate"].rolling(20).mean()
    return result


def market_state(frame: pd.DataFrame) -> dict[str, object]:
    enriched = add_indicators(frame)
    if len(enriched) < 120:
        raise ValueError("HS300 index needs at least 120 bars")
    row = enriched.iloc[-1]
    risk_on = bool(
        row["close"] > row["ma120"]
        and row["ma20"] > row["ma60"]
        and row["return5"] > -0.05
    )
    return {
        "trade_date": str(row["trade_date"]),
        "regime": "RISK_ON" if risk_on else "RISK_OFF",
        "risk_on": risk_on,
        "close": float(row["close"]),
        "ma20": float(row["ma20"]),
        "ma60": float(row["ma60"]),
        "ma120": float(row["ma120"]),
        "return5": float(row["return5"]),
    }


def candidate_metrics(frame: pd.DataFrame, benchmark_return20: float) -> dict[str, object] | None:
    if frame.empty or not {"close", "high", "low", "volume", "amount"}.issubset(frame.columns):
        return None
    enriched = add_indicators(frame)
    if len(enriched) < MIN_STOCK_BARS:
        return None
    row = enriched.iloc[-1]
    if row.isna().any() or int(row["trade_status"]) != 1 or int(row["is_st"]) == 1:
        return None
    price = float(row["close"])
    atr_ratio = float(row["atr14"] / price)
    relative_strength = float(row["return20"] - benchmark_return20)
    volume_ratio = float(row["volume"] / row["volume_median20"]) if row["volume_median20"] else 0
    distance_ma20 = float(price / row["ma20"] - 1)
    trend = bool(price > row["ma20"] > row["ma60"] > row["ma120"])
    breakout = bool(price >= row["previous_high20"])
    liquid = bool(row["amount_mean20"] >= 500_000_000)
    eligible_price = 5 <= price <= 100
    full_signal = bool(
        trend
        and breakout
        and liquid
        and eligible_price
        and relative_strength > 0.03
        and volume_ratio >= 1.2
        and 0.01 <= atr_ratio <= 0.045
        and distance_ma20 <= 0.08
    )
    score = 0.0
    score += 20 if trend else max(0, 10 - abs(distance_ma20) * 100)
    score += 20 if breakout else max(0, 20 - max(0, row["previous_high20"] / price - 1) * 200)
    score += min(15, max(0, (volume_ratio - 0.7) / 0.8 * 15))
    score += min(20, max(0, (relative_strength + 0.02) / 0.12 * 20))
    score += 15 if 0.01 <= atr_ratio <= 0.045 else 3
    score += max(0, 10 - max(0, distance_ma20 - 0.03) * 200)
    return {
        "trade_date": str(row["trade_date"]),
        "price": price,
        "atr14": float(row["atr14"]),
        "atr_ratio": atr_ratio,
        "relative_strength20": relative_strength,
        "volume_ratio": volume_ratio,
        "distance_ma20": distance_ma20,
        "amount_mean20": float(row["amount_mean20"]),
        "trend": trend,
        "breakout": breakout,
        "liquid": liquid,
        "eligible_price": eligible_price,
        "full_signal": full_signal,
        "technical_score": float(min(100, score)),
        "ma20": float(row["ma20"]),
    }


def planned_position(equity: float, price: float, atr: float) -> tuple[int, float]:
    stop_distance = min(2 * atr, price * 0.06)
    if stop_distance <= 0:
        return 0, price
    risk_shares = math.floor((equity * 0.005) / stop_distance / 100) * 100
    cap_shares = math.floor((equity * 0.20) / price / 100) * 100
    return max(0, min(risk_shares, cap_shares)), price - stop_distance


def reasons_for(metrics: dict[str, object]) -> list[str]:
    reasons = []
    if metrics["trend"]:
        reasons.append("均线多头排列")
    if metrics["breakout"]:
        reasons.append("收盘价突破20日高点")
    reasons.append(f"20日相对强弱 {metrics['relative_strength20']:+.1%}")
    reasons.append(f"成交量为20日中位数 {metrics['volume_ratio']:.2f} 倍")
    return reasons


def latest_financial_metrics(
    connection: sqlite3.Connection, code: str, trade_date: str
) -> dict[str, float]:
    rows = connection.execute(
        """
        SELECT data_type, metrics_json FROM financial_snapshots f
        WHERE code=? AND publish_date<=? AND report_period=(
          SELECT MAX(report_period) FROM financial_snapshots
          WHERE code=f.code AND data_type=f.data_type AND publish_date<=?
        )
        """,
        (code, trade_date, trade_date),
    ).fetchall()
    metrics: dict[str, float] = {}
    for row in rows:
        values = json.loads(row["metrics_json"])
        for key, value in values.items():
            if value is not None:
                metrics[f"{row['data_type']}_{key}"] = float(value)
    return metrics


def raw_factor_metrics(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    benchmark_return20: float,
) -> dict[str, object] | None:
    if frame.empty or len(frame) < MIN_STOCK_BARS:
        return None
    enriched = add_indicators(frame)
    row = enriched.iloc[-1]
    required = ["ma120", "atr14", "rsi14", "macd_hist", "volatility20"]
    if any(pd.isna(row.get(key)) for key in required):
        return None
    if int(row["trade_status"]) != 1 or int(row["is_st"]) == 1:
        return None
    joined = enriched[["trade_date", "daily_return"]].merge(
        benchmark[["trade_date", "daily_return"]], on="trade_date", suffixes=("", "_market")
    ).tail(60)
    market_variance = joined["daily_return_market"].var()
    beta60 = (
        joined["daily_return"].cov(joined["daily_return_market"]) / market_variance
        if market_variance and len(joined) >= 40
        else 1.0
    )
    correlation60 = joined["daily_return"].corr(joined["daily_return_market"])
    price = float(row["close"])
    values: dict[str, object] = {
        "trade_date": str(row["trade_date"]),
        "price": price,
        "ma20": float(row["ma20"]),
        "ma60": float(row["ma60"]),
        "ma120": float(row["ma120"]),
        "ma20_slope5": float(row["ma20_slope5"]),
        "return20": float(row["return20"]),
        "relative_strength20": float(row["return20"] - benchmark_return20),
        "atr14": float(row["atr14"]),
        "atr_ratio": float(row["atr14"] / price),
        "volume_ratio": float(row["volume"] / row["volume_median20"])
        if row["volume_median20"]
        else 0.0,
        "amount_mean20": float(row["amount_mean20"]),
        "amount_ratio5_20": float(row["amount_ratio5_20"]),
        "macd": float(row["macd"]),
        "macd_signal": float(row["macd_signal"]),
        "macd_hist": float(row["macd_hist"]),
        "macd_hist_change": float(row["macd_hist"] - enriched["macd_hist"].iloc[-2]),
        "rsi14": float(row["rsi14"]),
        "kdj_k": float(row["kdj_k"]),
        "kdj_d": float(row["kdj_d"]),
        "obv_slope10": float(row["obv_slope10"]),
        "mfi14": float(row["mfi14"]),
        "volatility20": float(row["volatility20"]),
        "downside_vol20": float(row["downside_vol20"]),
        "drawdown20": float(row["drawdown20"]),
        "up_down_volume_ratio": float(row["up_down_volume_ratio"])
        if pd.notna(row["up_down_volume_ratio"])
        else 1.0,
        "beta60": float(beta60) if pd.notna(beta60) else 1.0,
        "correlation60": float(correlation60) if pd.notna(correlation60) else 0.0,
        "breakout20": bool(price >= row["previous_high20"]),
        "distance_ma20": float(price / row["ma20"] - 1),
    }
    for column in ("turnover_rate", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"):
        value = row.get(column)
        values[column] = float(value) if value is not None and pd.notna(value) else None
    return values


def percentile(series: pd.Series, high_is_good: bool = True) -> pd.Series:
    result = series.rank(pct=True, method="average") * 100
    return result if high_is_good else 100 - result


def industry_percentile(frame: pd.DataFrame, column: str, high_is_good: bool) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="coerce")
    positive = numeric.where(numeric > 0)
    ranked = positive.groupby(frame["industry"].fillna("未分类")).rank(pct=True) * 100
    global_rank = positive.rank(pct=True) * 100
    group_sizes = frame.groupby(frame["industry"].fillna("未分类"))[column].transform("count")
    ranked = ranked.where(group_sizes >= 4, global_rank)
    if not high_is_good:
        ranked = 100 - ranked
    return ranked.fillna(20)


def score_factor_table(raw: pd.DataFrame) -> pd.DataFrame:
    result = raw.copy()
    rs_rank = percentile(result["relative_strength20"])
    volume_rank = percentile(result["volume_ratio"])
    amount_rank = percentile(result["amount_mean20"])
    vol_rank = percentile(result["volatility20"], high_is_good=False)
    downside_risk_rank = percentile(result["downside_vol20"])
    drawdown_risk_rank = percentile(-result["drawdown20"])

    result["trend_score"] = (
        (result["price"] > result["ma20"]).astype(float) * 20
        + (result["ma20"] > result["ma60"]).astype(float) * 20
        + (result["ma60"] > result["ma120"]).astype(float) * 15
        + (result["ma20_slope5"] > 0).astype(float) * 15
        + (result["macd"] > result["macd_signal"]).astype(float) * 15
        + (result["macd_hist_change"] > 0).astype(float) * 10
        + result["breakout20"].astype(float) * 5
    )
    rsi_quality = (100 - (result["rsi14"] - 60).abs() * 2).clip(0, 100)
    kdj_timing = (result["kdj_k"] > result["kdj_d"]).astype(float) * 100
    result["momentum_score"] = (rs_rank * 0.55 + rsi_quality * 0.30 + kdj_timing * 0.15)
    mfi_quality = (100 - (result["mfi14"] - 60).abs() * 2).clip(0, 100)
    obv_quality = percentile(result["obv_slope10"])
    up_down_quality = percentile(result["up_down_volume_ratio"])
    result["volume_score"] = (
        volume_rank * 0.20 + amount_rank * 0.20 + obv_quality * 0.25
        + mfi_quality * 0.20 + up_down_quality * 0.15
    )
    atr_quality = (100 - (result["atr_ratio"] - 0.025).abs() * 2500).clip(0, 100)
    beta_quality = (100 - (result["beta60"] - 1).abs() * 50).clip(0, 100)
    result["volatility_score"] = vol_rank * 0.45 + atr_quality * 0.35 + beta_quality * 0.20

    valuation_parts = pd.DataFrame(index=result.index)
    valuation_parts["pe"] = industry_percentile(result, "pe_ttm", False)
    valuation_parts["pb"] = industry_percentile(result, "pb_mrq", False)
    valuation_parts["ps"] = industry_percentile(result, "ps_ttm", False)
    valuation_parts["pcf"] = industry_percentile(result, "pcf_ncf_ttm", False)
    result["valuation_score"] = valuation_parts.mean(axis=1)

    def quality_score(row: pd.Series) -> float:
        points: list[float] = []
        mappings = {
            "profit_roeAvg": lambda x: np.clip(x / 0.20 * 100, 0, 100),
            "profit_npMargin": lambda x: np.clip(50 + x * 100, 0, 100),
            "growth_YOYNI": lambda x: np.clip(50 + x * 100, 0, 100),
            "growth_YOYPNI": lambda x: np.clip(50 + x * 100, 0, 100),
            "cash_flow_CFOToNP": lambda x: np.clip(x * 80, 0, 100),
            "balance_liabilityToAsset": lambda x: np.clip(100 - x * 100, 0, 100),
        }
        for key, transform in mappings.items():
            value = row.get(key)
            if value is not None and pd.notna(value):
                points.append(float(transform(float(value))))
        return float(np.mean(points)) if points else 50.0

    result["quality_score"] = result.apply(quality_score, axis=1)
    result["risk_score_base"] = (
        (
            (result["price"] < result["ma20"]).astype(float) * 35
            + (result["ma20"] < result["ma60"]).astype(float) * 30
            + (result["macd_hist"] < 0).astype(float) * 20
            + (result["macd_hist_change"] < 0).astype(float) * 15
        ) * 0.25
        + (downside_risk_rank * 0.55 + drawdown_risk_rank * 0.45) * 0.20
        + (
            percentile(-result["obv_slope10"]) * 0.40
            + percentile(-result["up_down_volume_ratio"]) * 0.35
            + (100 - mfi_quality) * 0.25
        ) * 0.20
        + (100 - percentile(result["amount_ratio5_20"])) * 0.15
        + (100 - result["quality_score"]) * 0.10
        + (100 - result["valuation_score"]) * 0.10
    ).clip(0, 100)
    result["opportunity_base"] = (
        result["trend_score"] * 0.20
        + result["momentum_score"] * 0.15
        + result["volume_score"] * 0.20
        + result["volatility_score"] * 0.10
        + result["valuation_score"] * 0.10
        + result["quality_score"] * 0.15
    ) / 0.90
    result["confidence"] = 60.0
    result["confidence"] += result["turnover_rate"].notna().astype(float) * 5
    valuation_complete = result[["pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"]].notna().mean(axis=1)
    result["confidence"] += valuation_complete * 15
    financial_columns = [column for column in result if column.startswith(("profit_", "growth_", "cash_flow_", "balance_"))]
    if financial_columns:
        result["confidence"] += result[financial_columns].notna().mean(axis=1) * 15
    result["confidence"] = result["confidence"].clip(0, 95)
    return result


def holding_recommendations(
    connection: sqlite3.Connection, portfolio: dict[str, object], market: dict[str, object]
) -> list[dict[str, object]]:
    results = []
    for position in portfolio["positions"]:
        frame = load_bars(connection, position["code"])
        frame = frame[frame["trade_date"] <= str(market["trade_date"])].reset_index(drop=True)
        if frame.empty:
            continue
        enriched = add_indicators(frame)
        latest = enriched.iloc[-1]
        control = connection.execute(
            "SELECT * FROM position_controls WHERE code=?", (position["code"],)
        ).fetchone()
        current_stop = float(control["current_stop"]) if control and control["current_stop"] else None
        highest = max(
            float(control["highest_close"]) if control and control["highest_close"] else 0,
            float(latest["close"]),
        )
        average = float(position["average_cost"])
        initial_stop = float(control["initial_stop"]) if control and control["initial_stop"] else None
        gain = float(latest["close"] / average - 1)
        below_ma20_twice = bool(
            len(enriched) >= 2
            and enriched["close"].iloc[-1] < enriched["ma20"].iloc[-1]
            and enriched["close"].iloc[-2] < enriched["ma20"].iloc[-2]
        )
        action = "HOLD"
        reasons = [f"持仓收益 {gain:+.1%}"]
        if current_stop and latest["close"] <= current_stop:
            action, reasons = "SELL", [f"收盘价已低于止损 {current_stop:.2f}"]
        elif below_ma20_twice:
            action, reasons = "SELL", ["连续两日收盘低于MA20"]
        elif market["regime"] == "RISK_OFF" and latest["close"] < latest["ma20"]:
            action, reasons = "SELL", ["市场风险较高且个股跌破MA20"]
        elif initial_stop and average - initial_stop > 0 and gain >= 2 * (average - initial_stop) / average:
            if not control or not control["partial_taken"]:
                action, reasons = "REDUCE", ["收益已达2R，减仓一半"]
            trailing = highest - 2 * float(latest["atr14"])
            current_stop = max(current_stop or 0, average, trailing)
        connection.execute(
            """
            UPDATE position_controls SET highest_close=?, current_stop=?, updated_at=CURRENT_TIMESTAMP
            WHERE code=?
            """,
            (highest, current_stop, position["code"]),
        )
        results.append(
            {
                "code": position["code"],
                "action": action,
                "score": None,
                "planned_price": float(latest["close"]),
                "quantity": position["quantity"] if action == "SELL" else position["quantity"] // 200 * 100 if action == "REDUCE" else 0,
                "stop_price": current_stop,
                "reasons": reasons,
                "metrics": {"gain": gain, "ma20": float(latest["ma20"])},
            }
        )
    return results


def enhanced_market_state(index_frame: pd.DataFrame, factors: pd.DataFrame) -> dict[str, object]:
    base = market_state(index_frame)
    index = add_indicators(index_frame).iloc[-1]
    above20 = float((factors["price"] > factors["ma20"]).mean())
    above60 = float((factors["price"] > factors["ma60"]).mean())
    positive20 = float((factors["return20"] > 0).mean())
    breakout_breadth = float(factors["breakout20"].mean())
    score = 0.0
    score += 10 if index["close"] > index["ma20"] else 0
    score += 10 if index["close"] > index["ma60"] else 0
    score += 15 if index["close"] > index["ma120"] else 0
    score += 10 if index["ma20"] > index["ma60"] else 0
    score += above20 * 15 + above60 * 10 + positive20 * 10 + breakout_breadth * 5
    score += max(0, min(10, (0.30 - float(index["volatility20"])) / 0.20 * 10))
    score += max(0, min(5, float(index["amount_ratio5_20"]) * 2.5))
    score = float(np.clip(score, 0, 100))
    regime = "RISK_ON" if score >= 65 else "CAUTIOUS" if score >= 50 else "RISK_OFF"
    return {
        **base,
        "regime": regime,
        "risk_on": regime == "RISK_ON",
        "market_score": score,
        "breadth_above_ma20": above20,
        "breadth_above_ma60": above60,
        "breadth_positive20": positive20,
        "breadth_breakout20": breakout_breadth,
        "volatility20": float(index["volatility20"]),
        "amount_ratio5_20": float(index["amount_ratio5_20"]),
    }


def opportunity_reasons(row: pd.Series) -> list[str]:
    groups = {
        "趋势": row["trend_score"],
        "动量": row["momentum_score"],
        "量价": row["volume_score"],
        "波动质量": row["volatility_score"],
        "估值": row["valuation_score"],
        "财务质量": row["quality_score"],
    }
    strongest = sorted(groups.items(), key=lambda item: item[1], reverse=True)[:3]
    reasons = [f"{name} {score:.0f}分" for name, score in strongest]
    if row.get("kronos_return") is not None and pd.notna(row.get("kronos_return")):
        reasons.append(f"Kronos 5日 {row['kronos_return']:+.1%}")
    return reasons


def risk_reasons(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if row["price"] < row["ma20"]:
        reasons.append("收盘低于MA20")
    if row["ma20"] < row["ma60"]:
        reasons.append("MA20低于MA60")
    if row["macd_hist"] < 0 and row["macd_hist_change"] < 0:
        reasons.append("MACD负向加速")
    if row["drawdown20"] < -0.08:
        reasons.append(f"20日回撤 {row['drawdown20']:.1%}")
    if row["amount_ratio5_20"] < 0.65:
        reasons.append("近5日成交额明显萎缩")
    if row["obv_slope10"] < 0:
        reasons.append("OBV下行")
    if row["pe_ttm"] is not None and row["pe_ttm"] <= 0:
        reasons.append("PE为负")
    if row.get("growth_YOYNI") is not None and pd.notna(row.get("growth_YOYNI")) and row["growth_YOYNI"] < -0.20:
        reasons.append("净利润同比下降超过20%")
    if row.get("kronos_return") is not None and pd.notna(row.get("kronos_return")) and row["kronos_return"] < -0.02:
        reasons.append(f"Kronos 5日 {row['kronos_return']:+.1%}")
    return reasons[:4] or ["综合风险分位于股票池前列"]


def run_strategy(connection: sqlite3.Connection, use_kronos: bool = False, top_k: int = 5) -> int:
    index_frame = load_bars(connection, INDEX_CODE)
    if index_frame.empty:
        raise RuntimeError("No HS300 index data. Run sync first.")
    benchmark = add_indicators(index_frame)
    target_trade_date = str(benchmark["trade_date"].iloc[-1])
    benchmark_return20 = float(benchmark["return20"].iloc[-1])
    portfolio = calculate_portfolio(connection)
    set_setting(connection, "peak_equity", portfolio["peak_equity"])

    candidates: list[dict[str, object]] = []
    frames: dict[str, pd.DataFrame] = {}
    for security in connection.execute(
        "SELECT code, name, industry FROM securities WHERE is_hs300=1 AND active=1 ORDER BY code"
    ):
        frame = load_bars(connection, security["code"])
        frame = frame[frame["trade_date"] <= target_trade_date].reset_index(drop=True)
        metrics = raw_factor_metrics(frame, benchmark, benchmark_return20)
        if not metrics or metrics["trade_date"] != target_trade_date:
            continue
        metrics.update(latest_financial_metrics(connection, security["code"], str(metrics["trade_date"])))
        frames[security["code"]] = frame
        candidates.append(
            {
                "code": security["code"], "name": security["name"],
                "industry": security["industry"] or "未分类", **metrics,
            }
        )
    if not candidates:
        raise RuntimeError("No eligible factor rows. Backfill extended daily data first.")
    factor_table = score_factor_table(pd.DataFrame(candidates))
    market = enhanced_market_state(index_frame, factor_table)
    factor_table["opportunity_score"] = factor_table["opportunity_base"]
    factor_table["risk_score"] = factor_table["risk_score_base"]
    factor_table = factor_table.sort_values("opportunity_base", ascending=False)

    kronos_used = False
    if use_kronos and kronos_available() and not factor_table.empty:
        try:
            from .kronos_service import KronosScorer

            scorer = KronosScorer()
        except Exception:
            scorer = None
        if scorer:
            for index, item in factor_table.head(max(top_k * 2, 10)).iterrows():
                try:
                    prediction = scorer.score(frames[str(item["code"])])
                    for key, value in prediction.items():
                        factor_table.at[index, key] = value
                    kronos_risk = float(np.clip(50 - prediction["kronos_return"] / 0.05 * 50, 0, 100))
                    factor_table.at[index, "opportunity_score"] = item["opportunity_base"] * 0.90 + prediction["kronos_score"] * 0.10
                    factor_table.at[index, "risk_score"] = item["risk_score_base"] * 0.90 + kronos_risk * 0.10
                    factor_table.at[index, "confidence"] = min(100, item["confidence"] + 5)
                    kronos_used = True
                except Exception as exc:
                    factor_table.at[index, "kronos_error"] = str(exc)
    factor_table = factor_table.sort_values("opportunity_score", ascending=False)

    drawdown = float(portfolio["drawdown"])
    position_count = len(portfolio["positions"])
    can_buy = bool(
        market["risk_on"] and drawdown > -0.05 and position_count < 3
        and portfolio["invested_ratio"] < 0.60
    )
    holdings = holding_recommendations(connection, portfolio, market)
    buy_count = 0
    selected = []
    for _, item in factor_table.head(top_k).iterrows():
        if len(selected) >= top_k:
            break
        action = "WATCH"
        quantity = 0
        stop = None
        eligible = bool(
            5 <= item["price"] <= 100
            and item["amount_mean20"] >= 500_000_000
            and item["opportunity_score"] >= 72
            and item["risk_score"] <= 35
            and item["confidence"] >= 70
        )
        if can_buy and eligible and buy_count < 1:
            quantity, stop = planned_position(float(portfolio["equity"]), item["price"], item["atr14"])
            if quantity >= 100:
                action = "BUY"
                buy_count += 1
        selected.append(
            {
                "code": item["code"],
                "action": action,
                "score": item["opportunity_score"],
                "planned_price": item["price"],
                "price_low": item["price"] * 0.985,
                "price_high": item["price"] * 1.02,
                "quantity": quantity,
                "stop_price": stop,
                "reasons": opportunity_reasons(item),
                "metrics": item.to_dict(),
            }
        )

    if any(item["action"] in {"SELL", "REDUCE"} for item in holdings):
        message = "优先处理持仓风险"
    elif buy_count:
        message = "一只股票满足完整买入条件"
    elif market["regime"] == "RISK_OFF":
        message = "市场风险较高，今日不开新仓"
    elif market["regime"] == "CAUTIOUS":
        message = "市场环境偏谨慎，今日不开新仓"
    else:
        message = "暂无完整买入信号，今日不操作"
    cursor = connection.execute(
        """
        INSERT INTO recommendation_runs(trade_date, status, market_regime, message, metrics_json)
        VALUES (?, 'COMPLETE', ?, ?, ?)
        """,
        (
            market["trade_date"], market["regime"], message,
            json.dumps(
                {
                    "market": market,
                    "portfolio": {k: v for k, v in portfolio.items() if k != "positions"},
                    "kronos": {"requested": bool(use_kronos), "used": kronos_used},
                },
                ensure_ascii=False,
            ),
        ),
    )
    run_id = int(cursor.lastrowid)
    group_columns = [
        "trend_score", "momentum_score", "volume_score", "volatility_score",
        "valuation_score", "quality_score",
    ]
    for _, item in factor_table.iterrows():
        metrics = item.to_dict()
        connection.execute(
            """
            INSERT INTO factor_snapshots(
              run_id, code, opportunity_score, risk_score, confidence,
              group_scores_json, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, item["code"], float(item["opportunity_score"]),
                float(item["risk_score"]), float(item["confidence"]),
                json.dumps({key: metrics.get(key) for key in group_columns}, default=str),
                json.dumps(metrics, ensure_ascii=False, default=str),
            ),
        )
    risk_table = factor_table.sort_values("risk_score", ascending=False).head(top_k)
    for rank, (_, item) in enumerate(risk_table.iterrows(), start=1):
        connection.execute(
            """
            INSERT INTO risk_alerts(
              run_id, code, rank, risk_score, level, reasons_json, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, item["code"], rank, float(item["risk_score"]),
                "HIGH_RISK" if item["risk_score"] >= 65 else "CAUTION",
                json.dumps(risk_reasons(item), ensure_ascii=False),
                json.dumps(item.to_dict(), ensure_ascii=False, default=str),
            ),
        )
    for rank, item in enumerate([*holdings, *selected], start=1):
        connection.execute(
            """
            INSERT INTO recommendations(
              run_id, code, action, rank, score, planned_price, price_low, price_high,
              quantity, stop_price, reasons_json, metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, item["code"], item["action"], rank, item.get("score"),
                item.get("planned_price"), item.get("price_low"), item.get("price_high"),
                item.get("quantity"), item.get("stop_price"),
                json.dumps(item.get("reasons", []), ensure_ascii=False),
                json.dumps(item.get("metrics", {}), ensure_ascii=False, default=str),
            ),
        )
    connection.commit()
    return run_id
