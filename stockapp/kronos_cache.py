from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import KRONOS_MODEL


DEFAULT_HORIZON = 5


def current_model_key(model_path: Path = KRONOS_MODEL) -> str:
    return str(model_path.expanduser().resolve())


def load_cached_prediction(
    connection: sqlite3.Connection,
    code: str,
    as_of_date: str,
    horizon: int = DEFAULT_HORIZON,
    model_key: str | None = None,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT * FROM kronos_predictions
        WHERE code=? AND as_of_date=? AND horizon=? AND model_key=?
        """,
        (code, as_of_date, horizon, model_key or current_model_key()),
    ).fetchone()
    if not row:
        return None
    return {
        "code": row["code"],
        "as_of_date": row["as_of_date"],
        "horizon": int(row["horizon"]),
        "model_key": row["model_key"],
        "kronos_return": float(row["predicted_return"]),
        "kronos_path_low": float(row["path_low"]),
        "kronos_score": float(row["score"]),
        "predicted_bars": json.loads(row["predicted_bars_json"]),
        "created_at": row["created_at"],
    }


def save_cached_prediction(
    connection: sqlite3.Connection,
    code: str,
    as_of_date: str,
    prediction: dict[str, object],
    horizon: int = DEFAULT_HORIZON,
    model_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO kronos_predictions(
          code, as_of_date, horizon, model_key, predicted_return,
          path_low, score, predicted_bars_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code, as_of_date, horizon, model_key) DO UPDATE SET
          predicted_return=excluded.predicted_return,
          path_low=excluded.path_low,
          score=excluded.score,
          predicted_bars_json=excluded.predicted_bars_json,
          created_at=CURRENT_TIMESTAMP
        """,
        (
            code,
            as_of_date,
            horizon,
            model_key or current_model_key(),
            float(prediction["kronos_return"]),
            float(prediction["kronos_path_low"]),
            float(prediction["kronos_score"]),
            json.dumps(prediction["predicted_bars"], ensure_ascii=False),
        ),
    )
