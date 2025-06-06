"""
daily_report_bot.py – minimal, debug-enabled, tested
Requires: python-telegram-bot >= 20, pandas, openpyxl
"""

import os, json, hashlib, logging, io
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

# ─── CONFIG ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.DEBUG
)
log = logging.getLogger("payout-bot")

ALLOWED_USER = 5419681514
BOT_TOKEN    = os.getenv("BOT_TOKEN")          # set in Railway variables
STATE_FILE   = "state.json"
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

WAIT_LINES, WAIT_TXT, WAIT_RESET = range(3)

# ─── small helpers ─────────────────────────────────────────
_load = lambda: json.loads(Path(STATE_FILE).read_text("utf-8")) if Path(STATE_FILE).exists() else {"hashes": []}
_save = lambda s: Path(STATE_FILE).write_text(json.dumps(s, indent=2))
_md5  = lambda p: hashlib.md5(p.read_bytes()).hexdigest()
_col  = lambda df, n: df[[c for c in df.columns if c.lower()==n.lower()][0]]

# ─── build payout summary ─────────────────────────────────
def build_summary(df: pd.DataFrame) -> str:
    bonuses   = float(_col(df, "Bonuses").iloc[0])
    wbonuses  = float(_col(df, "wBonuses").iloc[0])
    profit    = bonuses - wbonuses
    ivan_b = julian_b = round(profit * 0.35, 2)
    squad_b          = round(profit * 0.30, 2)

    labor_total = float(_col(df, "LaborTotal").iloc[0])
    mgmt        = float(_col(df, "Management").iloc[0])
    labor_exp   = float(_col(df, "LaborExp").iloc[0])
    squad_profit = labor_total - mgmt - labor_exp

    penalties_block = "* Worker_1 penalty: **- $5.00** *(⚠️ Doremon receives this)*"
    cross_name, cross_amt = "Doremon", 5.00

    return f"""
### 🧾 **Final Payout Summary**

**Bonuses:** ${bonuses:.2f}
**wBonuses (Worker Bonuses):** ${wbonuses:.2f}
**Bonus Profits:** ${bonuses:.2f} - ${wbonuses:.2f} = **${profit:.2f}**

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

# ─── error handler ────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Exception while handling update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("⚠️ An error occurred. Check logs.")
        except Exception:
            pass

# ─── command handlers ─────────────────────────────────────
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != ALLOWED_USER:
        return
    await u.message.reply_text(
        "Send .xlsx to create summary or use /split.",
        reply_markup=ReplyKeyboardMarkup([["/split", "/resetdb"]], resize_keyboard=True)
    )

async def reset_cmd(u: Update, *_):
    if u.effective_user.id != ALLOWED_USER:
        return
    await u.message.reply_text(
        "Reset DB?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Yes", callback_data="y"),
              InlineKeyboardButton("No",  callback_data="n")]]
        )
    )
    return WAIT_RESET

async def reset_btn(u: Update, *_):
    q = u.callback_query
    await q.answer()
    if q.data == "y":
        _save({"hashes": []})
        await q.edit_message_text("✅ DB cleared.")
    else:
        await q.edit_message_text("Cancelled.")
    return ConversationHandler.END

# ─── XLSX handler ─────────────────────────────────────────
async def xlsx_doc(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != ALLOWED_USER:
        return
    doc = u.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await u.message.reply_text("Need an .xlsx file.")
        return
    p = DOWNLOAD_DIR / doc.file_name
    await doc.get_file().download_to_drive(p)
    h = _md5(p)
    st = _load()
    if h in st["hashes"]:
        await u.message.reply_text("Already processed.")
        p.unlink()
        return
    try:
        df = pd.read_excel(p)
    except Exception as e:
        log.exception("Excel read failed")
        await u.message.reply_text(f"Read error: {e}")
        return
    await u.message.reply_text(build_summary(df), disable_web_page_preview=True)
    st["hashes"].append(h)
    _save(st)

# ─── split flow ───────────────────────────────────────────
async def split_cmd(u: Update, *_):
    if u.effective_user.id != ALLOWED_USER:
        return
    await u.message.reply_text("Lines per part?")
    return WAIT_LINES

async def split_lines(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(u.message.text.strip())
        assert n > 0
    except Exception:
        await u.message.reply_text("Positive integer please.")
        return WAIT_LINES
    c.user_data["n"] = n
    await u.message.reply_text("Send .txt file to split.")
    return WAIT_TXT

async def split_file(u: Update, c: ContextTypes.DEFAULT_TYPE):
    doc = u.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await u.message.reply_text("Need a .txt file.")
        return WAIT_TXT
    buf = io.BytesIO()
    await doc.get_file().download(out=buf)
    buf.seek(0)
    lines = buf.read().decode().splitlines(True)
    n = c.user_data["n"]
    parts = [lines[i:i+n] for i in range(0, len(lines), n)]
    msg = []
    for i, part in enumerate(parts, 1):
        await u.message.reply_document(
            InputFile(io.BytesIO("".join(part).encode()), filename=f"part{i}.txt")
        )
        msg.append(f"part{i}: {len(part)}")
    await u.message.reply_text("Split done:\n" + "\n".join(msg))
    return ConversationHandler.END

# ─── main entry ───────────────────────────────────────────
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var not set")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # simple handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), xlsx_doc))

    # conversations
    split_conv = ConversationHandler(
        entry_points=[CommandHandler("split", split_cmd)],
        states={
            WAIT_LINES: [MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), split_lines)],
            WAIT_TXT:   [MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), split_file)],
        },
        fallbacks=[]
    )
    reset_conv = ConversationHandler(
        entry_points=[CommandHandler("resetdb", reset_cmd)],
        states={WAIT_RESET: [CallbackQueryHandler(reset_btn)]},
        fallbacks=[]
    )
    app.add_handler(split_conv)
    app.add_handler(reset_conv)

    # global error handler
    app.add_error_handler(error_handler)

    log.info("✅ Polling started")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
