"""daily_report_bot.py — minimal version
Features:
  • Accept .xlsx breakdown → reply with Final Payout Summary (template fixed)
  • /split command → ask lines per part, receive .txt, split and send back
  • /resetdb to clear state (hash memory)
Only runs for Telegram ID 5419681514.
Requires: python-telegram-bot >= 20, pandas, openpyxl
"""

import os
import json
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
import io

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

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("payout-bot")

ALLOWED_USER = 5419681514
STATE_FILE = "state.json"  # hashes
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

# conv states
WAIT_LINES, WAIT_TXT, WAIT_XLSX, WAIT_RESET = range(4)

def _load():
    if not Path(STATE_FILE).exists():
        return {"hashes": []}
    return json.loads(Path(STATE_FILE).read_text("utf-8"))

def _save(s): Path(STATE_FILE).write_text(json.dumps(s, indent=2))

def _md5(p:Path):
    h=hashlib.md5(); h.update(p.read_bytes()); return h.hexdigest()

# ─── Report from sheet ─────────────────────────

def build_summary(df: pd.DataFrame) -> str:
    # assumes exact column names: Bonus, wBonus, LaborTotal, Mgmt, Labor, etc.
    bonuses = float(df.loc[0, "Bonuses"])
    wbonuses = float(df.loc[0, "wBonuses"])
    bonus_profit = bonuses - wbonuses
    ivan_b = julian_b = round(bonus_profit*0.35,2); squad_b = round(bonus_profit*0.30,2)

    labor_total = float(df.loc[0, "LaborTotal"])
    mgmt = float(df.loc[0, "Management"])
    labor_exp = float(df.loc[0, "LaborExp"])
    squad_profit = labor_total - mgmt - labor_exp

    # placeholders for penalties/cross checker (fill later manually)
    penalties_block = "* Worker_1 penalty: **- $5.00**"  # example static line
    cross_name = "Doremon"; cross_amount = 5.00

    summary = f"""### 🧾 **Final Payout Summary**\n\n**Bonuses:** ${bonuses:.2f}\n**wBonuses (Worker Bonuses):** ${wbonuses:.2f}\n**Bonus Profits:** ${bonuses:.2f} - ${wbonuses:.2f} = **${bonus_profit:.2f}**\n\n➡️ **Profit Split (from bonuses):**\n\n* **Me (35%)** = ${ivan_b:.2f}\n* **You (35%)** = ${julian_b:.2f}\n* **Support Squad (30%)** = ${squad_b:.2f}\n\n---\n\n**Labor Total:** ${labor_total:.2f}\n**Expenses:**\n\n* Management = ${mgmt:.2f}\n* Labor = ${labor_exp:.2f}\n\n➡️ **Support Squad Profit (from labor):**\n${labor_total:.2f} - ${mgmt:.2f} - ${labor_exp:.2f} = **${squad_profit:.2f}**\n\n---\n\n### 📌 **Other Adjustments**\n\n* **Cross Checker:** {cross_name}\n* **Penalties:**\n\n  {penalties_block}\n\n> 🟢 **{cross_name} gets ${cross_amount:.2f} penalty bonus. Notify him in the group & log this in Notion.**\n\n---\n\n### 💵 **Total Distribution**\n\n* **Ivan:** ${ivan_b:.2f}\n* **Julian:** ${julian_b:.2f} + ${mgmt:.2f} = **{ivan_b+mgmt:.2f}**\n* **Support Squad:** ${squad_b:.2f} + ${squad_profit:.2f} = **{squad_b+squad_profit:.2f}**\n* **Workers:** ${wbonuses:.2f} + ${labor_exp:.2f} = **{wbonuses+labor_exp:.2f}**\n* **Cross Checker ({cross_name}):** ${cross_amount:.2f}\n"""
    return summary

# ─── Handlers ──────────────────────────────────
async def cmd_start(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    await u.message.reply_text("Send .xlsx to create summary or use /split for txt split", reply_markup=ReplyKeyboardMarkup([["/split","/resetdb"]],resize_keyboard=True))

async def reset_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data="y"),InlineKeyboardButton("No",callback_data="n")]])
    await u.message.reply_text("Reset DB?",reply_markup=kb); return WAIT_RESET

async def reset_btn(u:Update,c:ContextTypes.DEFAULT_TYPE):
    q=u.callback_query; await q.answer()
    if q.data=="y": _save({"hashes":[]}); await q.edit_message_text("Cleared")
    else: await q.edit_message_text("Cancelled")
    return ConversationHandler.END

# receive xlsx
async def xlsx_doc(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    doc=u.message.document
    if not doc.file_name.lower().endswith(".xlsx"): return
    p=DOWNLOAD_DIR/doc.file_name; await doc.get_file().download_to_drive(p)
    h=_md5(p); st=_load()
    if h in st["hashes"]:
        await u.message.reply_text("Already processed"); p.unlink(); return
    df=pd.read_excel(p)
    summary=build_summary(df)
    await u.message.reply_text(summary, disable_web_page_preview=True)
    st["hashes"].append(h); _save(st)

# split flow
async def split_cmd(u:Update,c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    await u.message.reply_text("Lines per part?"); return WAIT_LINES

async def split_lines(u:Update,c:ContextTypes.DEFAULT_TYPE):
    try:n=int(u.message.text.strip()); assert n>0
    except: await u.message.reply_text("Positive int"); return WAIT_LINES
    c.user_data["n"]=n; await u.message.reply_text("Send .txt file"); return WAIT_TXT

async def split_file(u:Update,c:ContextTypes.DEFAULT_TYPE):
    doc=u.message.document
    if not doc.file_name.lower().endswith('.txt'):
        await u.message.reply_text("Need .txt"); return WAIT_TXT
    buf=io.BytesIO(); await doc.get_file().download(out=buf); buf.seek(0)
    lines=buf.read().decode().splitlines(True)
    n=c.user_data["n"]; parts=[lines[i:i+n] for i in range(0,len(lines),n)]
    summary=[]
    for i,part in enumerate(parts,1):
        data="".join(part).encode(); fn=f"part{i}.txt"
        await u.message.reply_document(InputFile(io.BytesIO(data),fn))
        summary.append(f"part{i}: {len(part)}")
    await u.message.reply_text("Split done:\n"+"\n".join(summary))
    return ConversationHandler.END

async def main():
    token=os.getenv("BOT_TOKEN"); app=ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("split",split_cmd))
    app.add_handler(CommandHandler("resetdb",reset_cmd))
    app.add_handler(CallbackQueryHandler(reset_btn))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), xlsx_doc))
    app.add_handler(ConversationHandler(entry_points=[split_cmd], states={WAIT_LINES:[MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), split_lines)], WAIT_TXT:[MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), split_file)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[reset_cmd], states={WAIT_RESET:[CallbackQueryHandler(reset_btn)]}, fallbacks=[]))
    log.info("✅ Polling started"); await app.run_polling()

if __name__ == "__main__":
    import asyncio; asyncio.run(main())
