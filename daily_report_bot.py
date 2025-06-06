
import os, json, hashlib, logging, re, io
from datetime import datetime
from pathlib import Path
import pandas as pd
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("daily-bot")

ALLOWED_USER = 5419681514
STATE_FILE = "state.json"
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)

WORKER_PAY, MGMT_CUT, SQUAD_MARGIN = 0.075, 0.02, 0.055
FULL_RATE = WORKER_PAY + MGMT_CUT + SQUAD_MARGIN

(WAIT_PENALTIES, WAIT_CROSS, WAIT_SPLIT_LINES, WAIT_SPLIT_FILE, WAIT_RESET) = range(5)

def _load():
    if not Path(STATE_FILE).exists():
        return {"hashes": [], "warn": {}}
    return json.loads(Path(STATE_FILE).read_text("utf-8"))

def _save(s): Path(STATE_FILE).write_text(json.dumps(s, indent=2))

def _md5(p): 
    h=hashlib.md5(); h.update(Path(p).read_bytes()); return h.hexdigest()

def _col(df, name): 
    cols=[c for c in df.columns if c.lower()==name.lower()]
    if not cols: raise KeyError(name)
    return df[cols[0]]

def _report(d):
    df=d["df"]
    role = _col(df,"Role")
    bonus= _col(df,"Bonus")
    bonuses_total=float(bonus.sum())
    wbonuses=float(bonus[role.str.lower()=="worker"].sum())
    bonus_profit=bonuses_total-wbonuses
    ivan_b=julian_b=round(bonus_profit*0.35,2)
    squad_b=round(bonus_profit*0.30,2)
    logs=int(_col(df,"LogCount").sum()) if "LogCount" in df else len(df)
    total=logs*FULL_RATE
    mgmt=logs*MGMT_CUT
    worker_pay=logs*WORKER_PAY
    squad_profit=total-mgmt-worker_pay
    cross=d.get("cross",0)
    squad_profit -= cross*WORKER_PAY
    pen_total=d.get("pen_total",0.0)
    ivan_pen=julian_pen=round(pen_total*0.35,2)
    squad_pen=round(pen_total*0.30,2)
    other=[f"{w} -${amt:.2f}" for w,amt in d.get("pen",[])]
    if cross:
        other.append(f"cross_checker -${cross*WORKER_PAY:.2f} ({cross} logs)")
    other_txt="None" if not other else "\n".join(other)
    st=_load()
    warnings_lines=[f"{w} {c}/3 warning" if c<3 else f"{w} 3/3 warnings – FIRED" for w,c in st["warn"].items()]
    warn_txt="None" if not warnings_lines else "\n".join(warnings_lines)
    ivan_total=ivan_b+ivan_pen
    julian_total=julian_b+mgmt+julian_pen
    squad_total=squad_b+squad_profit+squad_pen
    workers_total=wbonuses+worker_pay
    return (
        f"Bonuses: ${bonuses_total:.2f}\nwbonuses: ${wbonuses:.2f}\n\n"
        f"bonuses profits: ${bonuses_total:.2f} - ${wbonuses:.2f} = ${bonus_profit:.2f}\n\n"
        f"{bonus_profit:.2f} split to:\n35% Ivan=${ivan_b:.2f}\n35% Julian=${julian_b:.2f}\n30% Squad=${squad_b:.2f}\n—\n\n"
        f"Labor: ${total:.2f}\nExpenses:\n- Management=${mgmt:.2f}\n- Labor=${worker_pay:.2f}\n\n"
        f"Squad profit: ${squad_profit:.2f}\n—\n\n"
        f"Other:\n{other_txt}\n—\n\n"
        f"Warning count:\n{warn_txt}\n—\n\n"
        f"Total:\nIvan – ${ivan_total:.2f}\nJulian – ${julian_b:.2f}+${mgmt:.2f}=${julian_total:.2f}\n"
        f"Squad – ${squad_b:.2f}+${squad_profit:.2f}=${squad_total:.2f}\n"
        f"Workers – ${wbonuses:.2f}+${worker_pay:.2f}=${workers_total:.2f}\n\n"
        f"Generated: {datetime.now():%Y-%m-%d %H:%M}"
    )

def placeholder(): pass
