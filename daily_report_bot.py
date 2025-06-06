"""daily_report_bot.py — compact working version"""

import os, json, hashlib, logging, re
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

# ——— basic config ——————————————————————————
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s",
                    level=logging.INFO)
log = logging.getLogger("daily-bot")

ALLOWED_USER = 5419681514                 # your Telegram ID
STATE_FILE   = "state.json"
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

WORKER_PAY, MGMT_CUT, SQUAD_MARGIN = 0.075, 0.02, 0.055
FULL_RATE = WORKER_PAY + MGMT_CUT + SQUAD_MARGIN

(WAIT_PENALTIES, WAIT_CROSS,
 WAIT_SPLIT_LINES, WAIT_SPLIT_FILE,
 WAIT_RESET) = range(5)

# ——— persistence ————————————————————————————
def _load():
    if not Path(STATE_FILE).exists():
        return {"hashes": [], "warn": {}}
    return json.loads(Path(STATE_FILE).read_text("utf-8"))

def _save(s): Path(STATE_FILE).write_text(json.dumps(s, indent=2))

# ——— helpers ——————————————————————————————
_md5 = lambda p: hashlib.md5(Path(p).read_bytes()).hexdigest()
_col = lambda df, n: df[[c for c in df.columns if c.lower()==n.lower()][0]]

# ——— report builder —————————————————————————
def _report(d):
    df = d["df"]
    role  = _col(df,"Role")
    bonus = _col(df,"Bonus")

    bonuses_total = float(bonus.sum())
    wbonuses      = float(bonus[role.str.lower()=="worker"].sum())
    bonus_profit  = bonuses_total - wbonuses

    ivan_b = julian_b = round(bonus_profit*0.35,2)
    squad_b         = round(bonus_profit*0.30,2)

    logs = int(_col(df,"LogCount").sum()) if "LogCount" in df else len(df)
    total = logs*FULL_RATE
    mgmt  = logs*MGMT_CUT
    worker_pay = logs*WORKER_PAY
    squad_profit = total - mgmt - worker_pay

    cross = d.get("cross",0)
    squad_profit -= cross*WORKER_PAY

    pen_total = d.get("pen_total",0.0)
    ivan_pen = julian_pen = round(pen_total*0.35,2)
    squad_pen            = round(pen_total*0.30,2)

    other = [f"{w} -${amt:.2f}" for w,amt in d.get("pen",[])]
    if cross: other.append(f"cross_checker -${cross*WORKER_PAY:.2f} ({cross} logs)")
    other_txt = "None" if not other else "\n".join(other)

    st=_load()
    warn = [f"{w} {c}/3 warning" if c<3
            else f"{w} 3/3 warnings – FIRED"
            for w,c in st["warn"].items()]
    warn_txt = "None" if not warn else "\n".join(warn)

    ivan_tot = ivan_b+ivan_pen
    julian_tot = julian_b+mgmt+julian_pen
    squad_tot  = squad_b+squad_profit+squad_pen
    workers_tot= wbonuses+worker_pay

    return (
f"Bonuses: ${bonuses_total:.2f}\nwbonuses: ${wbonuses:.2f}\n\n"
f"bonuses profits: ${bonuses_total:.2f} - ${wbonuses:.2f} = "
f"${bonus_profit:.2f}\n\n"
f"{bonus_profit:.2f} split to:\n"
f"35% Ivan=${ivan_b:.2f}\n35% Julian=${julian_b:.2f}\n"
f"30% Squad=${squad_b:.2f}\n—\n\n"
f"Labor: ${total:.2f}\nExpenses:\n- Management=${mgmt:.2f}"
f"\n- Labor=${worker_pay:.2f}\n\n"
f"Squad profit: ${squad_profit:.2f}\n—\n\n"
f"Other:\n{other_txt}\n—\n\n"
f"Warning count:\n{warn_txt}\n—\n\n"
f"Total:\nIvan – ${ivan_tot:.2f}\nJulian – "
f"${julian_b:.2f}+${mgmt:.2f}=${julian_tot:.2f}\n"
f"Squad – ${squad_b:.2f}+${squad_profit:.2f}=${squad_tot:.2f}\n"
f"Workers – ${wbonuses:.2f}+${worker_pay:.2f}=${workers_tot:.2f}\n\n"
f"Generated: {datetime.now():%Y-%m-%d %H:%M}")
    )

# ——— Telegram handlers ——————————————————————
async def start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!=ALLOWED_USER: return
    await update.message.reply_text("Ready:",
        reply_markup=ReplyKeyboardMarkup([["/start","/split","/resetdb"]],
                                         resize_keyboard=True))

# reset DB
async def resetdb(update:Update,*_):
    if update.effective_user.id!=ALLOWED_USER: return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data="y"),
                              InlineKeyboardButton("No",callback_data="n")]])
    await update.message.reply_text("Reset DB?",reply_markup=kb)
    return WAIT_RESET

async def reset_btn(update:Update,*_):
    q=update.callback_query; await q.answer()
    if q.data=="y": _save({"hashes":[],"warn":{}}); txt="✅ cleared"
    else: txt="Cancelled"
    await q.edit_message_text(txt); return ConversationHandler.END

# daily-report flow
async def xlsx(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!=ALLOWED_USER: return
    doc=update.message.document
    if not doc.file_name.lower().endswith(".xlsx"): return
    p=DOWNLOAD_DIR/doc.file_name; await doc.get_file().download_to_drive(p)
    h=_md5(p); st=_load()
    if h in st["hashes"]:
        await update.message.reply_text("Already processed"); p.unlink(); return
    df=pd.read_excel(p); ctx.user_data["df"]=df; ctx.user_data["hash"]=h
    await update.message.reply_text("Penalties? ('None')")
    return WAIT_PENALTIES

async def pen(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    txt=update.message.text.strip()
    pen=[]; total=0.0
    if txt.lower()!="none":
        for ln in txt.splitlines():
            m=re.match(r"@?(\\w+)\\s+([0-9]+(?:\\.\\d+)?)",ln.strip())
            if not m: continue
            w,amt=m.groups(); amt=float(amt); pen.append((w,amt)); total+=amt
    ctx.user_data.update({"pen":pen,"pen_total":total})
    await update.message.reply_text("Cross-checker logs? (0)")
    return WAIT_CROSS

async def cross(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    try: c=int(update.message.text.strip()); assert c>=0
    except: await update.message.reply_text("Need integer"); return WAIT_CROSS
    ctx.user_data["cross"]=c
    rep=_report(ctx.user_data); await update.message.reply_text(rep)

    st=_load(); st["hashes"].append(ctx.user_data["hash"])
    for w,_ in ctx.user_data["pen"]:
        st["warn"][w]=st["warn"].get(w,0)+1
    _save(st); ctx.user_data.clear()
    return ConversationHandler.END

# split flow
async def split(update:Update,*_):
    if update.effective_user.id!=ALLOWED_USER:return
    await update.message.reply_text("Lines per part?"); return WAIT_SPLIT_LINES

async def split_lines(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    try:n=int(update.message.text.strip()); assert n>0
    except: await update.message.reply_text("Positive int"); return WAIT_SPLIT_LINES
    ctx.user_data["n"]=n; await update.message.reply_text("Send .txt file")
    return WAIT_SPLIT_FILE

async def split_file(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    doc=update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Need .txt"); return WAIT_SPLIT_FILE
    buf=io.BytesIO(); await doc.get_file().download(out=buf); buf.seek(0)
    lines=[l.decode() for l in buf.read().splitlines()]
    n=ctx.user_data["n"]; parts=[lines[i:i+n] for i in range(0,len(lines),n)]
    for i,part in enumerate(parts,1):
        fname=f\"part{i}.txt\"; data=\"\".join(part).encode()
        await update.message.reply_document(InputFile(io.BytesIO(data), fname))
    summary=\"\\n\".join([f\"part{i}: {len(p)}\" for i,p in enumerate(parts,1)])
    await update.message.reply_text(\"Split done:\\n\"+summary)
    ctx.user_data.clear(); return ConversationHandler.END

# ——— main ————————————————————————————————————
async def main():
    token=os.getenv(\"BOT_TOKEN\")
    if not token: raise RuntimeError(\"set BOT_TOKEN env\")
    app=ApplicationBuilder().token(token).build()

    # Conversations
    report_conv=ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), xlsx)],
        states={WAIT_PENALTIES:[MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), pen)],
                WAIT_CROSS:[MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), cross)]},
        fallbacks=[])
    split_conv=ConversationHandler(
        entry_points=[CommandHandler(\"split\", split)],
        states={WAIT_SPLIT_LINES:[MessageHandler(filters.TEXT & filters.User(ALLOWED_USER), split_lines)],
                WAIT_SPLIT_FILE:[MessageHandler(filters.Document.ALL & filters.User(ALLOWED_USER), split_file)]},
        fallbacks=[])
    reset_conv=ConversationHandler(
        entry_points=[CommandHandler(\"resetdb\", resetdb)],
        states={WAIT_RESET:[CallbackQueryHandler(reset_btn)]},
        fallbacks=[])

    app.add_handler(CommandHandler(\"start\", start))
    app.add_handler(report_conv)
    app.add_handler(split_conv)
    app.add_handler(reset_conv)

    log.info(\"✅ Polling started\"); await app.run_polling()

if __name__ == \"__main__\":             # for Railway
    import asyncio; asyncio.run(main())
"""

# write to file
Path("/mnt/data/daily_report_bot_compact.txt").write_text(script)
