import os
import io
import json
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ───────────────────────── CONFIG ─────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("daily-bot")

ALLOWED_USER = 5419681514  # only this Telegram user can interact

STATE_FILE = "state.json"  # simple JSON db (file hashes + warnings)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# economics per log
WORKER_PAY = 0.075
MANAGEMENT_CUT = 0.02
SUPPORT_MARGIN = 0.055
FULL_RATE = WORKER_PAY + MANAGEMENT_CUT + SUPPORT_MARGIN  # 0.15

# conversation steps (enumerate for clarity)
(
    WAIT_PENALTIES,
    WAIT_CROSSLOGS,
    WAIT_SPLIT_LINES,
    WAIT_SPLIT_FILE,
    WAIT_RESET_CONFIRM,
) = range(5)

# ──────────────────── tiny JSON helpers ────────────────────

def _load_state():
    if not Path(STATE_FILE).exists():
        return {"processed_hashes": [], "warnings": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.debug("State saved")

# ───────────────────────── utilities ─────────────────────────

def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_col(df: pd.DataFrame, name: str):
    matches = [c for c in df.columns if c.lower() == name.lower()]
    if not matches:
        raise KeyError(f"Missing column: {name}")
    return df[matches[0]]