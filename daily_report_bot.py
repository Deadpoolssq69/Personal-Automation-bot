# FIXED version of _build_daily_report with corrected conditional assignment

def _build_daily_report(data: dict) -> str:
    df: pd.DataFrame = data["df"]

    # Fetch columns ignoring case
    role_col = _get_col(df, "Role")

    # Determine which column is Bonus (case-insensitive)
    bonus_col_name = next((col for col in df.columns if col.lower() == "bonus"), None)
    if not bonus_col_name:
        raise KeyError("Missing 'Bonus' column")
    bonus_col = df[bonus_col_name]

    bonuses_total = float(bonus_col.sum())
    wbonuses = float(bonus_col[role_col.str.lower() == "worker"].sum())
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

    # cross-checker cost
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
        if count < 3:
            warning_lines.append(f"{worker} {count}/3 warning")
        else:
            warning_lines.append(f"{worker} 3/3 warnings – FIRED")
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
        f"Julian – ${julian_bonus_share:.2f} + ${management_expense:.2f} = ${julian_total:.2f}\n"
        f"Support Squad – ${squad_bonus_share:.2f} + ${support_profit:.2f} = ${squad_total:.2f}\n"
        f"Workers – ${wbonuses:.2f} + ${worker_labor_expense:.2f} = ${workers_total:.2f}"
        f"\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    return report