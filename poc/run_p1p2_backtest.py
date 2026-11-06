"""
Run the P1/P2 correlation engine on the Alfa export and write a review workbook.

    python -m poc.run_p1p2_backtest

Output: outputs/poc/p1p2_backtest.xlsx
  KPIs · Review sample (30 incidents, ops verdict dropdown) · Incidents · Members · Locations · Site codes · Unplaced
"""
import json
import time

import pandas as pd
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from poc import config as C
from poc.p1p2_engine import Settings, prepare, correlate


def main():
    t0 = time.time()
    st = Settings()
    prepared = prepare()
    t1 = time.time()
    r = correlate(prepared, st)
    t2 = time.time()
    k, inc, ev, nom = r["kpis"], r["incidents"], r["events"], prepared["nom"]

    print(f"Loaded and placed in {t1 - t0:.1f}s, correlated in {t2 - t1:.1f}s\n")
    for key, val in k.items():
        if key not in ("settings", "by_method"):
            print(f"  {key:34s} {val}")
    print(f"  by_method: {k['by_method']}")

    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    alerts = ev[ev["kind"].eq("alert")]
    multi = inc[inc["members"] >= 2].sort_values("members", ascending=False).copy()
    for col in ("teams_today", "child_teams"):
        multi[col] = multi[col].apply(", ".join)
    members = ev[ev["incident_id"].isin(multi["incident_id"])].sort_values(["incident_id", "created"])[
        ["incident_id", "event_id", "priority", "created", "closed", "location", "city", "loc_method", "loc_key", "device", "layer",
         "strength", "team", "rv_esxi", "rv_cluster", "runs_on_alert", "closed_by", "self_healed", "repeat_of", "title"]]
    review = multi[multi["devices"] >= 2].sample(min(30, len(multi)), random_state=42).sort_values("start")[
        ["incident_id", "location", "city", "start", "members", "p1", "devices", "layers", "root_title", "parent_team", "child_teams",
         "confidence", "why"]].copy()
    review["ops_verdict"], review["correct_root"], review["correct_parent_team"], review["comments"] = "", "", "", ""
    locations = alerts.groupby(alerts["location"].fillna("(unplaced)")).agg(
        alerts=("event_id", "size"), p1=("priority", lambda s: int((s == "P1").sum())),
        how_placed=("loc_method", lambda s: ", ".join(f"{m} {n}" for m, n in s.value_counts().items()))).reset_index()
    locations["incidents"] = locations["location"].map(inc.groupby("location").size()).fillna(0).astype(int)
    locations = locations.sort_values("alerts", ascending=False)
    site_codes = nom["site_codes"].copy()
    site_codes["alerts"] = site_codes["code"].map(alerts.loc[alerts["loc_method"].str.startswith("site code"), "loc_key"].value_counts()).fillna(0).astype(int)
    unplaced = alerts[alerts["location"].isna()]["signature"].value_counts().head(40).rename("alerts").reset_index()

    out = C.OUTPUT_DIR / "p1p2_backtest.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame([(a, b) for a, b in k.items() if a not in ("settings", "by_method")]
                     + [("by_method", json.dumps(k["by_method"])), ("settings", json.dumps(k["settings"]))],
                     columns=["KPI", "value"]).to_excel(xw, sheet_name="KPIs", index=False)
        review.to_excel(xw, sheet_name="Review sample", index=False)
        multi.to_excel(xw, sheet_name="Incidents", index=False)
        members.to_excel(xw, sheet_name="Members", index=False)
        locations.to_excel(xw, sheet_name="Locations", index=False)
        site_codes.to_excel(xw, sheet_name="Site codes", index=False)
        unplaced.to_excel(xw, sheet_name="Unplaced", index=False)
        if r.get("impact_links") is not None and len(r["impact_links"]):
            il = r["impact_links"].drop(columns=["kind"]).merge(r["impact"], on="impact_id").merge(
                inc[["incident_id", "location", "start", "members", "root_title", "parent_team"]], on="incident_id")
            il["symptom_layers"] = il["symptom_layers"].apply(", ".join)
            il.sort_values("start").to_excel(xw, sheet_name="Impact links", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for c in ws[1]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="C0392B")
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
