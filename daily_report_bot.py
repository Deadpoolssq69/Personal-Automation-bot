"""daily_report_bot.py — minimal & debug-enabled
Features:
  • /start -> instructions
  • Send .xlsx -> payout summary (fixed template)
  • /split -> ask lines -> accept .txt -> split
  • /resetdb -> Yes/No inline buttons
  • Only user 5419681514 can interact
  • Extensive DEBUG logs + global error handler
Requires: python-telegram-bot >= 20, pandas, openpyxl
"""

import os, json, hashlib, logging, re, io, traceback
from datetime import datetime
from pathlib import Path
import pandas as pd
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton,
    InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ─── BASIC CONFIG ──────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.DEBUG  # <— DEBUG globally
)
log = logging.getLogger("payout-bot")

ALLOWED_USER = 5419681514
BOT_TOKEN    = os.getenv("BOT_TOKEN")
STATE_FILE   = "state.json"
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

WAIT_PENALTY, WAIT_CROSS, WAIT_LINES, WAIT_TXT, WAIT_RESET = range(5)

# ─── HELPERS ───────────────────────────────────────────────
_load = lambda: json.loads(Path(STATE_FILE).read_text("utf-8")) if Path(STATE_FILE).exists() else {"hashes": []}
_save = lambda s: Path(STATE_FILE).write_text(json.dumps(s, indent=2))
_md5  = lambda p: hashlib.md5(p.read_bytes()).hexdigest()
_col  = lambda df,n: df[[c for c in df.columns if c.lower()==n.lower()][0]]

# ─── SUMMARY ───────────────────────────────────────────────

def build_summary(df: pd.DataFrame) -> str:
    log.debug("Building summary from dataframe shape=%s", df.shape)
    bonuses  = float(_col(df,"Bonuses").iloc[0])
    wbonuses = float(_col(df,"wBonuses").iloc[0])
    profit   = bonuses - wbonuses
    ivan_b = julian_b = round(profit*0.35,2); squad_b = round(profit*0.30,2)

    labor_total = float(_col(df,"LaborTotal").iloc[0])
    mgmt        = float(_col(df,"Management").iloc[0])
    labor_exp   = float(_col(df,"LaborExp").iloc[0])
    squad_profit = labor_total - mgmt - labor_exp

    penalties_block="* Worker_1 penalty: **- $5.00** *(⚠️ Doremon receives this)*"
    cross_name,cross_amt="Doremon",5.00

    return f"""### 🧾 **Final Payout Summary**\n\n**Bonuses:** ${bonuses:.2f}\n**wBonuses (Worker Bonuses):** ${wbonuses:.2f}\n**Bonus Profits:** ${bonuses:.2f} - ${wbonuses:.2f} = **${profit:.2f}**\n\n➡️ **Profit Split (from bonuses):**\n\n* **Me (35%)** = ${ivan_b:.2f}\n* **You (35%)** = ${julian_b:.2f}\n* **Support Squad (30%)** = ${squad_b:.2f}\n\n---\n\n**Labor Total:** ${labor_total:.2f}\n**Expenses:**\n* Management = ${mgmt:.2f}\n* Labor = ${labor_exp:.2f}\n\n➡️ **Support Squad Profit (from labor):**\n${labor_total:.2f} - ${mgmt:.2f} - ${labor_exp:.2f} = **${squad_profit:.2f}**\n\n---\n\n### 📌 **Other Adjustments**\n\n* **Cross Checker:** {cross_name}\n* **Penalties:**\n\n  {penalties_block}\n\n> 🟢 **{cross_name} gets ${cross_amt:.2f} penalty bonus. Notify him in the group & log this in Notion.**\n\n---\n\n### 💵 **Total Distribution**\n\n* **Ivan:** ${ivan_b:.2f}\n* **Julian:** ${julian_b:.2f} + ${mgmt:.2f} = **{ivan_b+mgmt:.2f}**\n* **Support Squad:** ${squad_b:.2f} + ${squad_profit:.2f} = **{squad_b+squad_profit:.2f}**\n* **Workers:** ${wbonuses:.2f} + ${labor_exp:.2f} = **{wbonuses+labor_exp:.2f}**\n* **Cross Checker ({cross_name}):** ${cross_amt:.2f}\n""".strip()

# ─── GLOBAL ERROR HANDLER ─────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Exception while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_user:
        try:
            await update.effective_chat.send_message("⚠️ An error occurred. Admin check the logs.")
        except Exception:  # chat might be unavailable
            pass

# ─── COMMANDS ─────────────────────────────────────────────
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    log.debug("/start from %s", u.effective_user.id)
    await u.message.reply_text("Send .xlsx or use /split", reply_markup=ReplyKeyboardMarkup([["/split","/resetdb"]], resize_keyboard=True))

async def reset_cmd(u:Update,*_):
    if u.effective_user.id!=ALLOWED_USER: return
    await u.message.reply_text("Reset DB?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data="y"),InlineKeyboardButton("No",callback_data="n")]]))
    return WAIT_RESET

async def reset_btn(u:Update,*_):
    q=u.callback_query; await q.answer()
    if q.data=="y": _save({"hashes":[]}); await q.edit_message_text("✅ DB cleared.")
    else: await q.edit_message_text("Cancelled")
    return ConversationHandler.END

# ─── XLSX handler ────────────────────────────────────────
async def xlsx_doc(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER:return
    doc=u.message.document; log.debug("Received file %s", doc.file_name)
    if not doc.file_name.lower().endswith(".xlsx"):
        await u.message.reply_text("Need .xlsx file"); return
    p=DOWNLOAD_DIR/doc.file_name; await doc.get_file().download_to_drive(p)
    h=_md5(p); st=_load()
    if h in st["hashes"]:
        await u.message.reply_text("Already processed"); p.unlink(); return
    try:
        df=pd.read_excel(p)
    except Exception as e:
        log.exception("Failed reading excel"); await u.message.reply_text(f"Read error: {e}"); return
    await u.message.reply_text(build_summary(df), disable_web_page_preview=True)
    st["hashes"].append(h); _save(st)

# ─── SPLIT flow ───────────────────────────────────────────
async def split_cmd(u:Update,*_):
    if u.effective_user.id!=ALLOWED_USER:return
    await u.message.reply_text("Lines per part?"); return WAIT_LINES

async def split_lines(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try:n=int(u.message.text.strip()); assert n>0
    except: await u.message.reply_text("Positive integer"); return WAIT_LINES
    c.user_data["n"]=n; log.debug("Split lines per part=%s", n)
    await u.message.reply_text("Send .txt file"); return WAIT_TXT

async def split_file(u:Update,c:ContextTypes.DEFAULT_TYPE):
    doc=u.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await u.message.reply_text("Need .txt"); return WAIT_TXT
    buf=io.BytesIO(); await doc.get_file().download(out=buf); buf.seek(0)
    lines=buf.read().decode().splitlines(True)
    n=c.user_data["n"]; parts=[lines[i:i+n] for i in range(0,len(lines),n)]
    log.debug("Splitting %d lines into %d parts", len(lines), len(parts))
    summary=[]
    for i,part in enumerate(parts,1):
        await u.message.reply_document(InputFile(io.BytesIO("".join(part).encode()),f"part{i}.txt"))
        summary.append(f"part{i}: {len(part)}")
    await u.message.reply_text("Split done:\n"+"\n".join(summary)); return ConversationHandler.END
