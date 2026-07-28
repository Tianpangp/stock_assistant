from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict

from .db import get_setting


@dataclass
class Position:
    code: str
    name: str
    quantity: int
    average_cost: float
    market_price: float | None
    market_value: float
    unrealized_pnl: float
    unrealized_return: float
    current_stop: float | None
    opened_date: str


def position_quantities(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT code, SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS quantity
        FROM transactions GROUP BY code
        """
    ).fetchall()
    return {row["code"]: int(row["quantity"] or 0) for row in rows}


def calculate_portfolio(connection: sqlite3.Connection) -> dict[str, object]:
    initial_capital = float(get_setting(connection, "initial_capital", "50000") or 50000)
    trades = connection.execute(
        """
        SELECT t.*, s.name FROM transactions t
        JOIN securities s ON s.code=t.code
        ORDER BY trade_date, id
        """
    ).fetchall()
    state: dict[str, dict[str, float | int | str]] = {}
    cash = initial_capital
    realized_pnl = 0.0
    for trade in trades:
        code = trade["code"]
        item = state.setdefault(
            code,
            {
                "name": trade["name"],
                "quantity": 0,
                "average_cost": 0.0,
                "opened_date": trade["trade_date"],
            },
        )
        quantity = int(trade["quantity"])
        price = float(trade["price"])
        fee = float(trade["fee"])
        held = int(item["quantity"])
        average = float(item["average_cost"])
        if trade["side"] == "BUY":
            new_quantity = held + quantity
            item["average_cost"] = (held * average + quantity * price + fee) / new_quantity
            item["quantity"] = new_quantity
            if held == 0:
                item["opened_date"] = trade["trade_date"]
            cash -= quantity * price + fee
        else:
            if quantity > held:
                raise ValueError(f"{code} sell quantity {quantity} exceeds held quantity {held}")
            realized_pnl += (price - average) * quantity - fee
            item["quantity"] = held - quantity
            cash += quantity * price - fee
            if item["quantity"] == 0:
                item["average_cost"] = 0.0

    latest_prices = {
        row["code"]: float(row["close"])
        for row in connection.execute(
            """
            SELECT b.code, b.close FROM daily_bars b
            JOIN (SELECT code, MAX(trade_date) trade_date FROM daily_bars GROUP BY code) x
              ON x.code=b.code AND x.trade_date=b.trade_date
            """
        )
    }
    controls = {
        row["code"]: row
        for row in connection.execute("SELECT * FROM position_controls")
    }
    positions: list[Position] = []
    market_value = unrealized = 0.0
    for code, item in state.items():
        quantity = int(item["quantity"])
        if quantity <= 0:
            continue
        average = float(item["average_cost"])
        market_price = latest_prices.get(code)
        value = quantity * market_price if market_price is not None else quantity * average
        pnl = value - quantity * average
        control = controls.get(code)
        positions.append(
            Position(
                code=code,
                name=str(item["name"]),
                quantity=quantity,
                average_cost=average,
                market_price=market_price,
                market_value=value,
                unrealized_pnl=pnl,
                unrealized_return=pnl / (quantity * average) if average else 0.0,
                current_stop=float(control["current_stop"])
                if control and control["current_stop"] is not None
                else None,
                opened_date=str(item["opened_date"]),
            )
        )
        market_value += value
        unrealized += pnl

    equity = cash + market_value
    peak = (
        initial_capital
        if not trades
        else float(get_setting(connection, "peak_equity", str(initial_capital)) or initial_capital)
    )
    if equity > peak:
        peak = equity
    drawdown = equity / peak - 1 if peak else 0.0
    return {
        "initial_capital": initial_capital,
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized,
        "invested_ratio": market_value / equity if equity else 0.0,
        "positions": [asdict(position) for position in sorted(positions, key=lambda p: p.code)],
    }


def record_transaction(
    connection: sqlite3.Connection,
    *,
    trade_date: str,
    code: str,
    side: str,
    quantity: int,
    price: float,
    fee: float = 0,
    stop_price: float | None = None,
    notes: str = "",
) -> int:
    side = validate_transaction_values(side, quantity, price, fee)
    security = connection.execute(
        "SELECT code FROM securities WHERE code=? AND kind='stock' AND is_verified=1",
        (code,),
    ).fetchone()
    if not security:
        raise ValueError("stock code has not been verified against the A-share market")
    held = position_quantities(connection).get(code, 0)
    if side == "SELL" and quantity > held:
        raise ValueError(f"sell quantity {quantity} exceeds held quantity {held}")

    cursor = connection.execute(
        """
        INSERT INTO transactions(trade_date, code, side, quantity, price, fee, stop_price, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trade_date, code, side, quantity, price, fee, stop_price, notes.strip()),
    )
    if side == "BUY" and held == 0:
        connection.execute(
            """
            INSERT INTO position_controls(code, opened_date, initial_stop, current_stop, highest_close)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
              opened_date=excluded.opened_date, initial_stop=excluded.initial_stop,
              current_stop=excluded.current_stop, highest_close=excluded.highest_close,
              partial_taken=0, updated_at=CURRENT_TIMESTAMP
            """,
            (code, trade_date, stop_price, stop_price, price),
        )
    if side == "SELL" and quantity == held:
        connection.execute("DELETE FROM position_controls WHERE code=?", (code,))
    elif side == "SELL":
        connection.execute(
            """
            UPDATE position_controls
            SET partial_taken=1, updated_at=CURRENT_TIMESTAMP WHERE code=?
            """,
            (code,),
        )
    connection.commit()
    return int(cursor.lastrowid)


def validate_transaction_values(
    side: str, quantity: int, price: float, fee: float
) -> str:
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity <= 0 or price <= 0 or fee < 0:
        raise ValueError("quantity and price must be positive; fee cannot be negative")
    if side == "BUY" and quantity % 100 != 0:
        raise ValueError("A-share buy quantity must be a multiple of 100")
    return side


def rebuild_position_control(connection: sqlite3.Connection, code: str) -> None:
    trades = connection.execute(
        "SELECT * FROM transactions WHERE code=? ORDER BY trade_date, id", (code,)
    ).fetchall()
    held = 0
    opened_date = None
    initial_stop = None
    opening_price = 0.0
    partial_taken = 0
    for trade in trades:
        quantity = int(trade["quantity"])
        if trade["side"] == "BUY":
            if held == 0:
                opened_date = trade["trade_date"]
                initial_stop = trade["stop_price"]
                opening_price = float(trade["price"])
                partial_taken = 0
            held += quantity
        else:
            held -= quantity
            if held < 0:
                raise ValueError(f"sell quantity creates a negative position for {code}")
            if held > 0:
                partial_taken = 1
            else:
                opened_date = None
                initial_stop = None
                opening_price = 0.0
                partial_taken = 0

    if held <= 0 or not opened_date:
        connection.execute("DELETE FROM position_controls WHERE code=?", (code,))
        return

    existing = connection.execute(
        "SELECT * FROM position_controls WHERE code=?", (code,)
    ).fetchone()
    highest_row = connection.execute(
        "SELECT MAX(close) highest FROM daily_bars WHERE code=? AND trade_date>=?",
        (code, opened_date),
    ).fetchone()
    highest = max(opening_price, float(highest_row["highest"] or 0))
    if existing and existing["opened_date"] == opened_date:
        highest = max(highest, float(existing["highest_close"] or 0))
        old_initial = existing["initial_stop"]
        old_current = existing["current_stop"]
        current_stop = (
            initial_stop
            if old_current is None or old_current == old_initial
            else old_current
        )
    else:
        current_stop = initial_stop
    connection.execute(
        """
        INSERT INTO position_controls(
          code, opened_date, initial_stop, current_stop, highest_close,
          partial_taken, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code) DO UPDATE SET
          opened_date=excluded.opened_date,
          initial_stop=excluded.initial_stop,
          current_stop=excluded.current_stop,
          highest_close=excluded.highest_close,
          partial_taken=excluded.partial_taken,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            code,
            opened_date,
            initial_stop,
            current_stop,
            highest,
            partial_taken,
        ),
    )


def update_transaction(
    connection: sqlite3.Connection,
    transaction_id: int,
    *,
    trade_date: str,
    code: str,
    side: str,
    quantity: int,
    price: float,
    fee: float = 0,
    stop_price: float | None = None,
    notes: str = "",
) -> None:
    original = connection.execute(
        "SELECT * FROM transactions WHERE id=?", (transaction_id,)
    ).fetchone()
    if not original:
        raise ValueError("transaction does not exist")
    side = validate_transaction_values(side, quantity, price, fee)
    security = connection.execute(
        "SELECT code FROM securities WHERE code=? AND kind='stock' AND is_verified=1",
        (code,),
    ).fetchone()
    if not security:
        raise ValueError("stock code has not been verified against the A-share market")

    connection.execute(
        """
        UPDATE transactions SET
          trade_date=?, code=?, side=?, quantity=?, price=?, fee=?,
          stop_price=?, notes=?
        WHERE id=?
        """,
        (
            trade_date,
            code,
            side,
            quantity,
            price,
            fee,
            stop_price,
            notes.strip(),
            transaction_id,
        ),
    )
    calculate_portfolio(connection)
    for affected_code in {str(original["code"]), code}:
        rebuild_position_control(connection, affected_code)
    connection.commit()
