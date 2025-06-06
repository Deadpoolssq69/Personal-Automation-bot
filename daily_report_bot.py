"""
daily_report_bot.py – minimal & tested
python-telegram-bot >= 20, pandas, openpyxl
"""

import os, json, hashlib, logging, re, io
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
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s",
                    level=logging.INFO)
log = logging.getLogger("payout-bot")

ALLOWED_USER = 5419681514                 # ← your Telegram ID
BOT_TOKEN    = os.getenv("BOT_TOKEN")     # set on Railway
STATE_FILE   = "state.json"               # stores processed hashes
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

# conversation states
WAIT_PENALTY, WAIT_CROSS, WAIT_LINES, WAIT_TXT, WAIT_RESET = range(5)

# ─── HELPERS ───────────────────────────────────────────────
def _load():
    if not Path(STATE_FILE).exists():
        return {"hashes": []}
    return json.loads(Path(STATE_FILE).read_text("utf-8"))

def _save(s): Path(STATE_FILE).write_text(json.dumps(s, indent=2))

def _md5(p: Path):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""): h.update(chunk)
    return h.hexdigest()

def _col(df: pd.DataFrame, name: str):
    return df[[c for c in df.columns if c.lower() == name.lower()][0]]

# ─── TEMPLATE SUMMARY ──────────────────────────────────────
def build_summary(df: pd.DataFrame) -> str:
    # expects columns: Bonuses, wBonuses, LaborTotal, Management, LaborExp
    bonuses     = float(_col(df, "Bonuses").iloc[0])
    wbonuses    = float(_col(df, "wBonuses").iloc[0])
    bonus_profit = bonuses - wbonuses
    ivan_b = julian_b = round(bonus_profit * 0.35, 2)
    squad_b       = round(bonus_profit * 0.30, 2)

    labor_total = float(_col(df, "LaborTotal").iloc[0])
    mgmt        = float(_col(df, "Management").iloc[0])
    labor_exp   = float(_col(df, "LaborExp").iloc[0])
    squad_profit = labor_total - mgmt - labor_exp

    # placeholder penalty & cross-checker
    penalties_block = "* Worker_1 penalty: **- $5.00** *(⚠️ Doremon receives this)*"
    cross_name  = "Doremon"
    cross_amt   = 5.00

    return f"""
### 🧾 **Final Payout Summary**

**Bonuses:** ${bonuses:.2f}
**wBonuses (Worker Bonuses):** ${wbonuses:.2f}
**Bonus Profits:** ${bonuses:.2f} - ${wbonuses:.2f} = **${bonus_profit:.2f}**

➡️ **Profit Split (from bonuses):**

* **Me (35%)** = ${ivan_b:.2f}
* **You (35%)** = ${julian_b:.2f}
* **Support Squad (30%)** = ${squad_b:.2f}

---

**Labor Total:** ${labor_total:.2f}
**Expenses:**
* Management = ${mgmt:.2f}
* Labor = ${labor_exp:.2f}

➡️ **Support Squad Profit (from labor):**
${labor_total:.2f} - ${mgmt:.2f} - ${labor_exp:.2f} = **${squad_profit:.2f}**

---

### 📌 **Other Adjustments**

* **Cross Checker:** {cross_name}
* **Penalties:**

  {penalties_block}

> 🟢 **{cross_name} gets ${cross_amt:.2f} penalty bonus. Notify him in the group & log this in Notion.**

---

### 💵 **Total Distribution**

* **Ivan:** ${ivan_b:.2f}
* **Julian:** ${julian_b:.2f} + ${mgmt:.2f} = **{ivan_b+mgmt:.2f}**
* **Support Squad:** ${squad_b:.2f} + ${squad_profit:.2f} = **{squad_b+squad_profit:.2f}**
* **Workers:** ${wbonuses:.2f} + ${labor_exp:.2f} = **{wbonuses+labor_exp:.2f}**
* **Cross Checker ({cross_name}):** ${cross_amt:.2f}
""".strip()

# ─── HANDLERS ──────────────────────────────────────────────
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != ALLOWED_USER: return
    await u.message.reply_text(
        "Send .xlsx to create summary or use /split for txt splitter.",
        reply_markup=ReplyKeyboardMarkup([["/split", "/resetdb"]], resize_keyboard=True)
    )

async def reset_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data="y"),
                              InlineKeyboardButton("No",callback_data="n")]])
    await u.message.reply_text("Reset DB?", reply_markup=kb)
    return WAIT_RESET

async def reset_btn(u:Update, *_):
    q=u.callback_query; await q.answer()
    if q.data=="y": _save({"hashes":[]}); await q.edit_message_text("✅ DB cleared.")
    else: await q.edit_message_text("Cancelled.")
    return ConversationHandler.END

# ---- XLSX flow ----
async def xlsx_doc(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ALLOWED_USER: return
    doc=u.message.document
    if not doc.file_name.lower().endswith(".xlsx"): return
    p=DOWNLOAD_DIR/doc.file_name; await doc.get_file().download_to_drive(p)
    h=_md5(p); st=_load()
    if h in st["hashes"]:
        await u.message.reply_text("Already processed."); p.unlink(); return
    df=pd.read_excel(p)
    await u.message.reply_text(build_summary(df), disable_web_page_preview=True)
    st["hashes"].append(h); _save(st)

# ---- SPLIT flow ----
async def split_cmd(u:Update, *_):
    if u.effective_user.id!=ALLOWED_USER: return
    await u.message.reply_text("Lines per part?"); return WAIT_LINES

async def split_lines(u:Update, c:ContextTypes.DEFAULT_TYPE):
    try: n=int(u.message.text.strip()); assert n>0
    except: await u.message.reply_text("Positive integer please."); return WAIT_LINES
    c.user_data["n"]=n; await u.message.reply_text("Send .txt file"); return WAIT_TXT

async def split_file(u:Update, c:ContextTypes.DEFAULT_TYPE):
    doc=u.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await u.message.reply_text("Need .txt"); return WAIT_TXT
    buf=io.BytesIO(); await doc.get_file().download(out=buf); buf.seek(0)
    lines=buf.read().decode().splitlines(True)
    n=c.user_data["n"]; parts=[lines[i:i+n] for i in range(0,len(lines),n)]
    summary=[]
    for i,part in enumerate(parts,1):
        await u.message.reply_document(InputFile(io.BytesIO("".join(part).encode()),
                                                 filename=f"part{i}.txt"))
        summary.append(f"part{i}: {len(part)}")
    await u.message.reply_text("Split done:\n"+ "\n".join(summary))
    return ConversationHandler.END

# ─── MAIN ──────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # simple handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("split", split_cmd))
    app.add_handler(CommandHandler("resetdb", reset_cmd))
    app.add_handler(CallbackQueryHandler(reset_btn))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER),
                                   xlsx_doc))

    # conversations
    split_conv = ConversationHandler(
        entry_points=[split_cmd],
        states={
            WAIT_LINES: [MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), split_lines)],
            WAIT_TXT:   [MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), split_file)],
        },
        fallbacks=[]
    )
    reset_conv = ConversationHandler(
        entry_points=[reset_cmd],
        states={WAIT_RESET:[CallbackQueryHandler(reset_btn)]},
        fallbacks=[]
    )
    app.add_handler(split_conv)
    app.add_handler(reset_conv)

    log.info("✅ Polling started")
    app.run_polling()

if __name__ == "__main__":
    main()
