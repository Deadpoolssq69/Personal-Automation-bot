"""daily_report_bot.py — minimal, debug, _column-tolerant_, with /stop
• Handles payout summary (.xlsx) and /split (.txt)
• Accepts column variants (Bonus/Bonuses, wbonus, etc.)
• /resetdb clears processed hashes
• NEW /stop stops the bot (only for ALLOWED_USER)
Requires: python-telegram-bot >= 20, pandas, openpyxl
"""

import os, json, hashlib, logging, io
from pathlib import Path
import pandas as pd
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.DEBUG)
log = logging.getLogger("payout-bot")

ALLOWED_USER = 5419681514
BOT_TOKEN    = os.getenv("BOT_TOKEN")
STATE_FILE   = "state.json"
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

WAIT_LINES, WAIT_TXT, WAIT_RESET = range(3)

_load = lambda: json.loads(Path(STATE_FILE).read_text("utf-8")) if Path(STATE_FILE).exists() else {"hashes": []}
_save = lambda s: Path(STATE_FILE).write_text(json.dumps(s, indent=2))
_md5  = lambda p: hashlib.md5(p.read_bytes()).hexdigest()

def _col(df: pd.DataFrame, wanted: str):
    """Return column matching *wanted* (case-insensitive, singular/plural)
    Accepts first 5 letters match to allow Bonus / Bonuses, wbonus, etc.
    Raises KeyError with clear list if not found."""
    wanted_clean = wanted.lower()[:5]
    for c in df.columns:
        if c.lower().replace(' ', '')[:5] == wanted_clean:
            return df[c]
    raise KeyError(f"Missing column '{wanted}'. Found: {list(df.columns)[:10]}")

# ── SUMMARY BUILDER ─────────────────────────────────────

def build_summary(df: pd.DataFrame) -> str:
    bonuses  = float(_col(df, "Bonuses").iloc[0])
    wbonus   = float(_col(df, "wBonuses").iloc[0])
    profit   = bonuses - wbonus
    ivan = julian = round(profit*0.35,2); squad = round(profit*0.30,2)
    labor_total = float(_col(df,"LaborTotal").iloc[0])
    mgmt = float(_col(df,"Management").iloc[0])
    labor_exp = float(_col(df,"LaborExp").iloc[0])
    squad_profit = labor_total - mgmt - labor_exp
    return (f"Bonuses ${bonuses:.2f}\nwBonuses ${wbonus:.2f}\nProfit ${profit:.2f}\n"
            f"Ivan {ivan:.2f}  Julian {julian:.2f}  Squad {squad:.2f}\n"
            f"Labor total ${labor_total:.2f} → Squad profit ${squad_profit:.2f}")

# ── ERROR HANDLER ───────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Exception", exc_info=context.error)
    msg = str(context.error)[:300]
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(f"⚠️ Error: {msg}")
        except Exception:
            pass

# ── COMMANDS ────────────────────────────────────────────
async def cmd_start(u:Update,*_):
    if u.effective_user.id!=ALLOWED_USER:return
    await u.message.reply_text("Send .xlsx or /split", reply_markup=ReplyKeyboardMarkup([["/split","/resetdb","/stop"]],resize_keyboard=True))

async def stop_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER:return
    await u.message.reply_text("👋 Stopping bot…")
    c.application.stop()

# ── RESET DB FLOW ──────────────────────────────────────
async def reset_cmd(u:Update,*_):
    if u.effective_user.id!=ALLOWED_USER:return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data="y"),InlineKeyboardButton("No",callback_data="n")]])
    await u.message.reply_text("Reset DB?", reply_markup=kb); return WAIT_RESET
async def reset_btn(u:Update,*_):
    q=u.callback_query; await q.answer()
    if q.data=="y": _save({"hashes":[]}); await q.edit_message_text("✅ DB cleared")
    else: await q.edit_message_text("Cancelled")
    return ConversationHandler.END

# ── XLSX RECEIVER ──────────────────────────────────────
async def xlsx_doc(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER:return
    doc=u.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await u.message.reply_text("Need .xlsx"); return
    p=DOWNLOAD_DIR/doc.file_name; await doc.get_file().download_to_drive(p)
    h=_md5(p); st=_load()
    if h in st["hashes"]:
        await u.message.reply_text("Already processed"); p.unlink(); return
    try:
        df=pd.read_excel(p)
        await u.message.reply_text(build_summary(df))
        st["hashes"].append(h); _save(st)
    except Exception as e:
        log.exception("Processing error")
        await u.message.reply_text(f"⚠️ Error: {e}")

# ── SPLIT FLOW ──────────────────────────────────────────
async def split_cmd(u:Update,*_):
    if u.effective_user.id!=ALLOWED_USER:return
    await u.message.reply_text("Lines per part?"); return WAIT_LINES
async def split_lines(u:Update, c:ContextTypes.DEFAULT_TYPE):
    try:n=int(u.message.text.strip()); assert n>0
    except: await u.message.reply_text("Positive int"); return WAIT_LINES
    c.user_data["n"]=n; await u.message.reply_text("Send .txt"); return WAIT_TXT
async def split_file(u:Update,c:ContextTypes.DEFAULT_TYPE):
    doc=u.message.document
    if not doc.file_name.lower().endswith('.txt'):
        await u.message.reply_text("Need .txt"); return WAIT_TXT
    buf=io.BytesIO(); await doc.get_file().download(out=buf); buf.seek(0)
    lines=buf.read().decode().splitlines(True)
    n=c.user_data["n"]; parts=[lines[i:i+n] for i in range(0,len(lines),n)]
    for i,p in enumerate(parts,1):
        await u.message.reply_document(InputFile(io.BytesIO("".join(p).encode()),f"part{i}.txt"))
    await u.message.reply_text("Split done"); return ConversationHandler.END

# ── MAIN (sync) ─────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), xlsx_doc))

    split_conv=ConversationHandler(
        entry_points=[CommandHandler("split", split_cmd)],
        states={WAIT_LINES:[MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), split_lines)], WAIT_TXT:[MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), split_file)]},
        fallbacks=[])
    reset_conv=ConversationHandler(
        entry_points=[CommandHandler("resetdb", reset_cmd)],
        states={WAIT_RESET:[CallbackQueryHandler(reset_btn)]},
        fallbacks=[])
    app.add_handler(split_conv); app.add_handler(reset_conv)

    log.info("✅ Polling started"); app.run_polling()

if __name__=='__main__':
    main()
