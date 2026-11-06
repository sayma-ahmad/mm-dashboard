"""
Run the correlation engine on the exports and write a review workbook.

    python -m poc.run_backtest

Output: outputs/poc/poc_backtest.xlsx  (KPIs, incidents, members, user-ticket links, SN<->AN links, review sample)
"""
import json
import time

import pandas as pd
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from poc import config as C
from poc.engine import Settings, load_raw, prepare, correlate


def main():
    t0 = time.time()
    st = Settings()
    prepared = prepare(load_raw())
    t1 = time.time()
    r = correlate(prepared, st)
    t2 = time.time()
    k, inc, ev = r["kpis"], r["incidents"], r["events"]

    print(f"Loaded and enriched in {t1 - t0:.1f}s, correlated in {t2 - t1:.1f}s\n")
    for key, val in k.items():
        if key != "settings":
            print(f"  {key:28s} {val}")

    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    multi = inc[inc["members"] >= 2].sort_values("members", ascending=False).copy()
    for col in ("teams_today", "child_teams"):
        multi[col] = multi[col].apply(", ".join)
    members = ev[ev["incident_id"].isin(multi["incident_id"])].sort_values(["incident_id", "created"])[
        ["incident_id", "event_id", "source", "kind", "created", "closed", "site", "city", "device", "layer",
         "strength", "team_today", "closed_by", "self_healed", "repeat_of", "title"]]
    review = multi[multi["devices"] >= 2].sample(min(30, len(multi)), random_state=42).sort_values("start")[
        ["incident_id", "site", "city", "start", "members", "devices", "layers", "root_title", "parent_team", "child_teams", "why"]].copy()
    review["ops_verdict"], review["correct_root"], review["correct_parent_team"], review["comments"] = "", "", "", ""

    out = C.OUTPUT_DIR / "poc_backtest.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame([(a, b) for a, b in k.items() if a != "settings"] + [("settings", json.dumps(k["settings"]))],
                     columns=["KPI", "value"]).to_excel(xw, sheet_name="KPIs", index=False)
        review.to_excel(xw, sheet_name="Review sample", index=False)
        multi.to_excel(xw, sheet_name="Incidents", index=False)
        members.to_excel(xw, sheet_name="Members", index=False)
        r["user_links"].to_excel(xw, sheet_name="User ticket links", index=False)
        r["sn_an_links"].to_excel(xw, sheet_name="SN-AN links", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for c in ws[1]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2A6FDB")
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col[:300]) + 2, 60)
        ws = xw.book["Review sample"]
        col = ws.cell(row=1, column=list(review.columns).index("ops_verdict") + 1).column_letter
        dv = DataValidation(type="list", formula1='"Correct,Should be split,Not sure"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{len(review) + 1}")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
