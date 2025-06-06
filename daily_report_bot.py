import os
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
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# ───────────────────────── CONFIG ─────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ALLOWED_USER = 5419681514  # your fixed Telegram user‑id

STATE_FILE = "state.json"  # persistent JSON db (file hashes + warnings)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Economics per log
WORKER_PAY = 0.075
MANAGEMENT_CUT = 0.02
SUPPORT_MARGIN = 0.055
FULL_RATE = WORKER_PAY + MANAGEMENT_CUT + SUPPORT_MARGIN  # = 0.15

# Conversation states
WAIT_PENALTIES, WAIT_CROSSLOGS, WAIT_RESET_CONFIRM = range(3)

# ──────────────────── Tiny JSON DB helpers ────────────────────

def _load_state():
    if not Path(STATE_FILE).exists():
        return {"processed_hashes": [], "warnings": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    logger.debug("State saved ✔️")

# ───────────────────────── Bot handlers ─────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(
        "👋 Ready! Send today’s breakdown .xlsx and I’ll build the Daily Report.",
        reply_markup=ReplyKeyboardMarkup([["/start", "/resetdb"]], resize_keyboard=True),
    )


async def resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes – wipe everything", callback_data="RESETDB_YES")],
            [InlineKeyboardButton("No, keep data", callback_data="RESETDB_NO")],
        ]
    )
    await update.message.reply_text(
        "⚠️ This will delete all stored hashes and warning counts. Continue?",
        reply_markup=kb,
    )
    return WAIT_RESET_CONFIRM


async def resetdb_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "RESETDB_YES":
        _save_state({"processed_hashes": [], "warnings": {}})
        await query.edit_message_text("✅ Database reset.")
        logger.info("DB reset on user request")
    else:
        await query.edit_message_text("✖️ Reset cancelled.")
    return ConversationHandler.END


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("Please send an .xlsx breakdown file.")
        return

    file = await doc.get_file()
    dl_path = DOWNLOAD_DIR / doc.file_name
    await file.download_to_drive(dl_path)
    logger.info("Downloaded %s", dl_path.name)

    file_hash = _file_md5(dl_path)
    state = _load_state()
    if file_hash in state["processed_hashes"]:
        await update.message.reply_text("⚠️ This file was already processed. Aborting.")
        dl_path.unlink(missing_ok=True)
        return

    # load excel
    try:
        df = pd.read_excel(dl_path)
    except Exception as exc:
        logger.exception("Failed reading Excel")
        await update.message.reply_text(f"Error reading file: {exc}")
        return

    context.user_data.update({"df": df, "file_hash": file_hash})

    await update.message.reply_text(
        "Any penalties today? Reply one per line (e.g. forcefev -5). Send 'None' if none."
    )
    return WAIT_PENALTIES


async def receive_penalties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    penalties, total_penalty = [], 0.0
    if not (len(lines) == 1 and lines[0].lower() == "none"):
        for line in lines:
            m = re.match(r"@?(\w+)\s+[-$]?([0-9]+(?:\.[0-9]+)?)", line)
            if not m:
                await update.message.reply_text(f"Could not parse line: {line}\nPlease resend penalties list.")
                return WAIT_PENALTIES
            worker, amt_str = m.groups()
            amt = float(amt_str)
            penalties.append((worker, amt))
            total_penalty += amt
    context.user_data.update({"penalties": penalties, "penalty_total": total_penalty})
    await update.message.reply_text("How many logs did the cross-checker handle today? (0 if none)")
    return WAIT_CROSSLOGS


async def receive_crosslogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cross_logs = int(update.message.text.strip())
        if cross_logs < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a non‑negative integer.")
        return WAIT_CROSSLOGS

    context.user_data["cross_logs"] = cross_logs

    report = _build_daily_report(context.user_data)
    await update.message.reply_text(report)

    # persist file hash & warnings
    state = _load_state()
    state["processed_hashes"].append(context.user_data["file_hash"])
    for worker, _ in context.user_data.get("penalties", []):
        key = worker.lower()
        state["warnings"].setdefault(key, 0)
        state["warnings"][key] += 1
    _save_state(state)

    context.user_data.clear()
    return ConversationHandler.END

# ───────────────────────── Utility helpers ─────────────────────────

def _file_md5(path: Path) -> str:
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def _get_col(df: pd.DataFrame, name: str):
    matches = [c for c in df.columns if c.lower() == name.lower()]
    if not matches:
        raise KeyError(f"Missing column: {name}")
    return df[matches[0]]


def _build_daily_report(data: dict) -> str:
    df: pd.DataFrame = data["df"]

    role_col = _get_col(df, "Role")
    # find bonus column case‑insensitively
    bonus_col_name = next((c for c in df.columns if c.lower() == "bonus"), None)
    if bonus_col_name is None:
        raise KeyError("Missing 'Bonus' column in sheet")
    bonus_col = df[bonus_col_name]

    bonuses_total = float(bonus_col.sum())
    wbonuses = float(bonus_col[role_col.str.lower() == "worker"].sum())
    bonus_profit = bonuses_total - wbonuses

    # bonus split
    ivan_bonus_share = round(bonus_profit * 0.35, 2)
    julian_bonus_share = round(bonus_profit * 0.35, 2)
    squad_bonus_share = round(bonus_profit * 0.30, 2)

    # logs
    logs = int(_get_col(df, "LogCount").sum()) if any(c.lower() == "logcount" for c in df.columns) else len(df)

    total_labor_pool = logs * FULL_RATE
    management_expense = logs * MANAGEMENT_CUT
    worker_labor_expense = logs * WORKER_PAY
    support_profit = total_labor_pool - management_expense - worker_labor_expense

    # cross‑checker cost
    cross_logs = data.get("cross_logs", 0)
    cross_cost = cross_logs * WORKER_PAY
    support_profit -= cross_cost

    # penalties
    penalty_total = data.get("penalty_total", 0.0)
    ivan_penalty = round(penalty_total * 0.35, 2)
    julian_penalty = round(penalty_total * 0.35, 2)
    squad_penalty = round(penalty_total * 0.30, 2)

    # Other section
    other_lines = [f"{w} -${amt:.2f}" for w, amt in data.get("penalties", [])]
    if cross_logs:
        other_lines.append(f"cross_checker logs -${cross_cost:.2f} ({cross_logs} logs)")
    other_text = "None" if not other_lines else "\n".join(other_lines)

    # warnings section
    state = _load_state()
    warnings = []
    for worker, count in state.get("warnings", {}).items():
        if count < 3:
            warnings.append(f"{worker} {count}/3 warning")
        else:
            warnings.append(f"{worker} 3/3 warnings – FIRED")
    warnings_text = "None" if not warnings else "\n".join(warnings)

    # totals
    ivan_total = ivan_bonus_share + ivan_pen
