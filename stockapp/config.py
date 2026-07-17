from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DATA_DIR = Path(os.environ.get("STOCK_ASSISTANT_DATA", PROJECT_ROOT / "data"))
DATABASE_PATH = Path(os.environ.get("STOCK_ASSISTANT_DB", DATA_DIR / "stocks.db"))
LOG_DIR = PROJECT_ROOT / "logs"


def configured_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


KRONOS_SOURCE = configured_path(
    "KRONOS_SOURCE", WORKSPACE_ROOT / "test_for_Kronos" / "Kronos"
)
KRONOS_MODEL = configured_path(
    "KRONOS_MODEL", WORKSPACE_ROOT / "kronos_models" / "Kronos-base-Finetuned"
)
KRONOS_TOKENIZER = configured_path(
    "KRONOS_TOKENIZER", WORKSPACE_ROOT / "kronos_models" / "Kronos-tokenizer-Finetuned"
)
KRONOS_ENABLED = os.environ.get("KRONOS_ENABLED", "auto").strip().lower()

INITIAL_HISTORY_DATE = os.environ.get("STOCK_HISTORY_START", "2018-01-01")
INITIAL_CAPITAL = float(os.environ.get("STOCK_INITIAL_CAPITAL", "50000"))


def kronos_available() -> bool:
    if KRONOS_ENABLED in {"0", "false", "no", "off", "disabled"}:
        return False
    return all(
        path.exists() for path in (KRONOS_SOURCE, KRONOS_MODEL, KRONOS_TOKENIZER)
    )


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
