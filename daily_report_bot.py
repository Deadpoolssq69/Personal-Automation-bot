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

# ───────────────────────── CONFIG / CONSTANTS ─────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ALLOWED_USER = 5419681514  # fixed Telegram user‑ID

STATE_FILE = "state.json"  # persistent memory (processed hashes + warnings)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# economics per log
WORKER_PAY = 0.075
MANAGEMENT_CUT = 0.02
SUPPORT_MARGIN = 0.055
FULL_RATE = WORKER_PAY + MANAGEMENT_CUT + SUPPORT_MARGIN  # 0.15

# conversation steps
WAIT_PENALTIES, WAIT_CROSSLOGS, WAIT_RESET_CONFIRM = range(3)

# ──────────────────────── tiny JSON DB helpers ───────────────────────

def _load_state():
    if not Path(STATE_FILE).exists():
        return {"processed_hashes": [], "warnings": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    logger.debug("State saved")

# ───────────────────────────── BOT HANDLERS ──────────────────────────

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
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes – wipe everything", callback_data="RESETDB_YES")],
            [InlineKeyboardButton("No, keep data", callback_data="RESETDB_NO")],
        ]
    )
    await update.message.reply_text(
        "⚠️ This will delete all stored hashes and warning counts. Continue?",
        reply_markup=keyboard,
    )
    return WAIT_RESET_CONFIRM


async def resetdb_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "RESETDB_YES":
        _save_state({"processed_hashes": [], "warnings": {}})
        await query.edit_message_text("✅ Database reset.")
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
    logger.info("Downloaded %s", dl_path)

    md5 = _file_md5(dl_path)
    state = _load_state()
    if md5 in state["processed_hashes"]:
        await update.message.reply_text("⚠️ This file was already processed. Aborting.")
        dl_path.unlink(missing_ok=True)
        return

    try:
        df = pd.read_excel(dl_path)
    except Exception as e:
        logger.exception("Failed reading xlsx")
        await update.message.reply_text(f"Error reading file: {e}")
        return

    context.user_data.update({"df": df, "file_hash": md5})

    await update.message.reply_text(
        "Any penalties today? (one per line, e.g. forcefev -5)\nSend 'None' if no penalties."
    )
    return WAIT_PENALTIES


async def receive_penalties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lines = [l for l in text.splitlines() if l.strip()]

    penalties = []  # list[(worker, amount)]
    total_penalty = 0.0
    if not (len(lines) == 1 and lines[0].lower() == "none"):
        for line in lines:
            m = re.match(r"(?:@?)(\w+)\s+[-$]?([0-9]+(?:\.[0-9]+)?)", line, re.I)
            if not m:
                await update.message.reply_text(f"Could not parse: {line}\nPlease resend.")
                return WAIT_PENALTIES
            worker, amt_str = m.groups()
            amt = float(amt_str)
            penalties.append((worker, amt))
            total_penalty += amt
    context.user_data.update({"penalties": penalties, "penalty_total": total_penalty})

    await update.message.reply_text("How many logs did the cross‑checker handle today? (0 if none)")
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

    # build and send report
    report = _build_daily_report(context.user_data)
    await update.message.reply_text(report)

    # persist file hash & warnings
    state = _load_state()
    state["processed_hashes"].append(context.user_data["file_hash"])
    # update persistent warnings
    for worker, _ in context.user_data.get("penalties", []):
        state["warnings"].setdefault(worker.lower(), 0)
        state["warnings"][worker.lower()] += 1
    _save_state(state)

    context.user_data.clear()
    return ConversationHandler.END

# ───────────────────────────── UTILITIES ─────────────────────────────

def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_col(df: pd.DataFrame, name: str):
    """Return column by name, case‑insensitive, raising KeyError if missing."""
    match = [c for c in df.columns if c.lower() == name.lower()]
    if not match:
        raise KeyError(name)
    return df[match[0]]


def _build_daily_report(data: dict) -> str:
    df: pd.DataFrame = data["df"]

    # Fetch columns ignoring case
    bonuses_total = float(_get_col(df, "Bonus").sum())
    role_col = _get_col(df, "Role")
    wbonuses = float(df[role_col.str.lower() == "worker"][role_col.name.replace("Role", "Bonus")].sum()) if "Bonus" in df.columns else wbonuses = float(_get_col(df, "bonus").loc[role_col.str.lower() == "worker"].sum())
    bonus_profit = bonuses_total - wbonuses

    # splits
    ivan_bonus_share = round(bonus_profit * 0.35, 2)
    julian_bonus_share = round(bonus_profit * 0.35, 2)
    squad_bonus_share = round(bonus_profit * 0.30, 2)

    # log count
    if any(c.lower() == "logcount" for c in df.columns):
        logs = int(_get_col(df, "LogCount").sum())
    else:
        logs = len(df)

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
    ivan_penalty = penalty_total * 0.35
    julian_penalty = penalty_total * 0.35
    squad_penalty = penalty_total * 0.30

    # Other section lines
    other_lines = [f"{w} -${amt:.2f}" for w, amt in data.get("penalties", [])]
    if cross_logs:
        other_lines.append(f"cross_checker logs -${cross_cost:.2f} ({cross_logs} logs)")
    other_text = "None" if not other_lines else "\n".join(other_lines)

    # warnings section (persistent)
    state = _load_state()
    warning_lines = []
    for worker, count in state.get("warnings", {}).items():
        warning_lines.append(f"{worker} {count}/3 warning{'s' if count != 1 else ''}")
    warnings_text = "None" if not warning_lines else "\n".join(warning_lines)

    # totals
    ivan_total = ivan_bonus_share + ivan_penalty
    julian_total = julian_bonus_share + management_expense + julian_penalty
    squad_total = squad_bonus_share + support_profit + squad_penalty
    workers_total = wbonuses + worker_labor_expense

    report = (
        f"Bonuses: ${bonuses_total:.2f}\n"
        f"wbonuses: ${wbonuses:.2f}\n\n"
        f"bonuses profits: ${bonuses_total:.2f} - ${wbonuses:.2f} = ${bonus_profit:.2f}\n\n"
        f"{bonus_profit:.2f} split to:\n"
        f"35 % me = ${ivan_bonus_share:.2f}\n"
        f"35 % you = ${julian_bonus_share:.2f}\n"
        f"30 % support squad = ${squad_bonus_share:.2f}\n—\n\n"
        f"Labor: ${total_labor_pool:.2f}\n"
        f"Expenses:\n"
        f"- Management = ${management_expense:.2f}\n"
        f"- Labor = ${worker_labor_expense:.2f}\n\n"
        f"Support Squad profit: ${total_labor_pool:.2f} - ${management_expense:.2f} - {worker_labor_expense:.2f} = ${support_profit:.2f}\n—\n\n"
        f"Other:\n{other_text}\n—\n\n"
        f"Warning count:\n{warnings_text}\n—\n\n"
        f"Total:\n"
        f"Ivan – ${ivan_total:.2f}\n"
        f"Julian – ${julian_bonus_share:.2f} + ${management_expense
