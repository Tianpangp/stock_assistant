from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from .config import KRONOS_MODEL, KRONOS_SOURCE, KRONOS_TOKENIZER


FEATURES = ["open", "high", "low", "close", "volume", "amount"]


class KronosScorer:
    def __init__(self, device: str = "auto", context: int = 512, horizon: int = 5) -> None:
        if not KRONOS_SOURCE.exists():
            raise FileNotFoundError(f"Kronos source missing: {KRONOS_SOURCE}")
        sys.path.insert(0, str(KRONOS_SOURCE))
        from model import Kronos, KronosPredictor, KronosTokenizer

        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        tokenizer = KronosTokenizer.from_pretrained(str(KRONOS_TOKENIZER))
        model = Kronos.from_pretrained(str(KRONOS_MODEL))
        tokenizer.eval()
        model.eval()
        self.predictor = KronosPredictor(model, tokenizer, device=device, max_context=context)
        self.context = context
        self.horizon = horizon

    def score(self, frame: pd.DataFrame) -> dict[str, object]:
        context = frame.tail(self.context).copy()
        if len(context) < self.context:
            raise ValueError(f"Kronos needs {self.context} daily bars")
        timestamps = pd.to_datetime(context["trade_date"])
        future = pd.bdate_range(timestamps.iloc[-1] + pd.Timedelta(days=1), periods=self.horizon)
        with torch.inference_mode():
            predicted = self.predictor.predict(
                df=context[FEATURES].reset_index(drop=True),
                x_timestamp=timestamps.reset_index(drop=True),
                y_timestamp=pd.Series(future),
                pred_len=self.horizon,
                T=1.0,
                top_k=1,
                top_p=1.0,
                sample_count=1,
                verbose=False,
            )
        start = float(context["close"].iloc[-1])
        endpoint = float(predicted["close"].iloc[-1])
        predicted_return = endpoint / start - 1
        path_low = float(predicted["low"].min()) / start - 1
        predicted_values = predicted[FEATURES].to_numpy(dtype=float)
        if not np.isfinite(predicted_values).all() or not np.isfinite(
            [endpoint, predicted_return, path_low]
        ).all():
            raise ValueError("Kronos returned a non-finite prediction")
        score = max(0.0, min(100.0, 50.0 + predicted_return / 0.05 * 50.0))
        predicted_bars = []
        for timestamp, row in predicted.iterrows():
            predicted_bars.append(
                {
                    "time": pd.Timestamp(timestamp).date().isoformat(),
                    **{feature: float(row[feature]) for feature in FEATURES},
                }
            )
        return {
            "kronos_return": predicted_return,
            "kronos_path_low": path_low,
            "kronos_score": score,
            "predicted_bars": predicted_bars,
        }
