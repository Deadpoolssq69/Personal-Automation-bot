import os
import json
import hashlib
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ───────────────────────── CONSTANTS ─────────────────────────
ALLOWED_USER = 5419681514  # your fixed Telegram user‑ID
STATE_FILE = "state.json"  # persistent memory (processed files / logs)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# per‑log economics
WORKER_PAY = 0.075   # $ to workers per log
MANAGEMENT_CUT = 0.02  # $ to management per log
SUPPORT_MARGIN = 0.055  # $ retained by Support Squad per log
FULL_RATE = WORKER_PAY + MANAGEMENT_CUT + SUPPORT_MARGIN  # 0.15

# conversation states
WAIT_PENALTIES, WAIT_CROSSLOGS = range(2)

# ────────────────────── helper: tiny JSON DB ──────────────────

def _load_state():
    if not Path(STATE_FILE).exists():
        return {"processed_hashes": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# ───────────────────────── bot commands ──────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(
        "👋 Ready! Send today’s breakdown .xlsx and I’ll build the Daily Report.",
        reply_markup=ReplyKeyboardMarkup([["/start"]], resize_keyboard=True, one_time_keyboard=True),
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("Please send an .xlsx breakdown file.")
        return

    # download file
    file = await doc.get_file()
    dl_path = DOWNLOAD_DIR / doc.file_name
    await file.download_to_drive(dl_path)

    # duplicate check (md5)
    md5 = _file_md5(dl_path)
    state = _load_state()
    if md5 in state["processed_hashes"]:
        await update.message.reply_text(
            "⚠️ This file was already processed. Aborting to prevent double billing."
        )
        dl_path.unlink(missing_ok=True)
        return

    # read with pandas
    try:
        df = pd.read_excel(dl_path)
    except Exception as e:
        await update.message.reply_text(f"Error reading file: {e}")
        dl_path.unlink(missing_ok=True)
        return

    context.user_data.update({
        "df": df,
        "file_hash": md5,
    })

    # ask for penalties
    await update.message.reply_text(
        "Any penalties today? Reply with one per line, e.g.\nforcefev -5\nwolf_ironclaw -1\nSend 'None' if no penalties."
    )
    return WAIT_PENALTIES


async def receive_penalties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lines = [l for l in text.splitlines() if l.strip()]

    penalties = []  # list[(str, float)]
    total_penalty = 0.0
    if not (len(lines) == 1 and lines[0].lower() == "none"):
        for line in lines:
            m = re.search(r"-?\$?([0-9]+(?:\.[0-9]+)?)", line)
            if not m:
                await update.message.reply_text(f"Could not parse: {line}\nPlease resend penalties list.")
                return WAIT_PENALTIES
            amt = float(m.group(1))  # positive magnitude
            penalties.append((line, amt))
            total_penalty += amt
    context.user_data.update({
        "penalties": penalties,
        "penalty_total": total_penalty,
    })

    # ask cross‑checker logs
    await update.message.reply_text(
        "How many logs did the cross‑checker handle today? (0 if none)"
    )
    return WAIT_CROSSLOGS


async def receive_crosslogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cross_logs = int(update.message.text.strip())
        if cross_logs < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a non‑negative integer for cross‑checker logs.")
        return WAIT_CROSSLOGS

    context.user_data["cross_logs"] = cross_logs

    report = _build_daily_report(context.user_data)
    await update.message.reply_text(report)

    # mark file as processed
    state = _load_state()
    state["processed_hashes"].append(context.user_data["file_hash"])
    _save_state(state)

    context.user_data.clear()
    return ConversationHandler.END

# ─────────────────────── utility functions ───────────────────

def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_daily_report(data: dict) -> str:
    df: pd.DataFrame = data["df"]

    # derive bonuses
    bonuses_total = float(df["Bonus"].sum())
    wbonuses = float(df[df["Role"].str.lower() == "worker"]["Bonus"].sum())
    bonus_profit = bonuses_total - wbonuses

    # bonus splits
    ivan_bonus_share = round(bonus_profit * 0.35, 2)
    julian_bonus_share = round(bonus_profit * 0.35, 2)
    squad_bonus_share = round(bonus_profit * 0.30, 2)

    # log count
    logs = int(df["LogCount"].sum()) if "LogCount" in df.columns else len(df)

    total_labor_pool = logs * FULL_RATE
    management_expense = logs * MANAGEMENT_CUT
    worker_labor_expense = logs * WORKER_PAY
    support_profit = total_labor_pool - management_expense - worker_labor_expense

    # cross‑checker
    cross_logs = data.get("cross_logs", 0)
    cross_cost = cross_logs * WORKER_PAY
    support_profit -= cross_cost

    # penalties
    penalty_total = data.get("penalty_total", 0.0)
    ivan_penalty = penalty_total * 0.35
    julian_penalty = penalty_total * 0.35
    squad_penalty = penalty_total * 0.30

    # Other section
    other_lines = [p for p, _ in data.get("penalties", [])]
    if cross_logs:
        other_lines.append(f"cross_checker logs -${cross_cost:.2f} ({cross_logs} logs)")
    other_text = "None" if not other_lines else "\n".join(other_lines)

    # totals
    ivan_total = ivan_bonus_share + ivan_penalty
    julian_total = julian_bonus_share + management_expense + julian_penalty
    squad_total = squad_bonus_share + support_profit + squad_penalty
    workers_total = wbonuses + worker_labor_expense

    # build report string
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
        f"Total:\n"
        f"Ivan – ${ivan_total:.2f}\n"
        f"Julian – ${julian_bonus_share:.2f} + ${management_expense:.2f} = ${julian_total:.2f}\n"
        f"Support Squad – ${squad_bonus_share:.2f} + ${support_profit:.2f} = ${squad_total:.2f}\n"
        f"Workers – ${wbonuses:.2f} + ${worker_labor_expense:.2f} = ${workers_total:.2f}"
        f"\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    return report

# ─────────────────────────── main ────────────────────────────

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable")

    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL & filters.Chat(ALLOWED_USER), handle_document)],
        states={
            WAIT_PENALTIES: [MessageHandler(filters.TEXT & filters.Chat(ALLOWED_USER), receive_penalties)],
            WAIT_CROSSLOGS: [MessageHandler(filters.TEXT & filters.Chat(ALLOWED_USER), receive_crosslogs)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    app.run_polling()


if __name__ == "__main__":
    main()
