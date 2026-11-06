"""
Demo console for the Mahindra alert-correlation POC.

    streamlit run poc/app.py

Runs the correlation engine on the Alfa + Msolve exports and shows the incidents it would have created,
with the primary problem, the parent team and the child teams. Settings in the sidebar re-run it live.
"""
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poc import config as C                                   # noqa: E402
from poc.engine import Settings, load_raw, load_site_master, prepare, correlate   # noqa: E402

st.set_page_config(page_title="Alert Correlation POC", page_icon="🔗", layout="wide")

ORANGE, BLUE, GREY = "#e4572e", "#2a6fdb", "#9aa5b1"


@st.cache_data(show_spinner="Loading Alfa and Msolve exports (first time only, ~15 s)...")
def get_prepared():
    return prepare(load_raw())


@st.cache_data(show_spinner="Correlating...")
def get_result(settings_tuple):
    return correlate(get_prepared(), Settings(*settings_tuple))


def join(v):
    return ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v))


# ------------------------------------------------------------------ sidebar
st.sidebar.header("Correlation rules")
window = st.sidebar.slider("Group window (minutes between alerts)", 5, 60, 15, 5,
                           help="An alert joins a group if it arrives within this many minutes of the previous one at the same plant.")
level = st.sidebar.radio("Group alerts by", ["site", "city"], format_func={"site": "Plant code (e.g. NAGP1)", "city": "City (e.g. Nagpur)"}.get)
dedupe = st.sidebar.slider("Repeat window (same device + same problem)", 0, 30, 10, 5)
flap = st.sidebar.slider("Self-healed if auto-closed within (minutes)", 0, 15, 5, 1)
after = st.sidebar.slider("Attach user tickets up to N min after last alert", 15, 180, 60, 15)
fallback = st.sidebar.checkbox("What-if: group alerts with no device by identical text",
                               help="Shows roughly what host names would unlock. Riskier: identical alerts from different firewalls can be merged.")
if fallback:
    st.sidebar.warning("What-if mode: results are an upper bound, not a recommendation.")

settings = (window, level, dedupe, fallback, flap, 10, after)
try:
    r = get_result(settings)
except FileNotFoundError as e:
    st.error(f"Input file not found: {e.filename}. Put the exports in {C.INPUT_DIR} or change FILES in poc/config.py.")
    st.stop()
k, inc, ev = r["kpis"], r["incidents"], r["events"]

st.sidebar.header("Filters")
dmin, dmax = ev["created"].min().date(), ev["created"].max().date()
start_default = max(dmin, pd.Timestamp("2026-10-01").date())
dates = st.sidebar.date_input("Dates", (start_default, dmax), min_value=dmin, max_value=dmax)
d0, d1 = (dates if isinstance(dates, tuple) and len(dates) == 2 else (start_default, dmax))
plants = sorted(inc["site"].dropna().unique())
pick_plants = st.sidebar.multiselect("Plants", plants)
min_members = st.sidebar.slider("Show incidents with at least N alerts", 2, 50, 5)

# ------------------------------------------------------------------ header + KPIs
st.title("Alert Correlation POC")
st.caption("Alfa (AN) + Msolve (SN), 1–15 Oct 2026 · alerts grouped by plant and time · primary problem → parent team, other teams notified")

c = st.columns(3)
c[0].metric("Alerts & tickets in", f"{k['events_in']:,}")
c[1].metric("Incidents out", f"{k['incidents_out']:,}", f"-{k['ticket_reduction_pct']}%", delta_color="inverse")
c[2].metric("Storms (5+ alerts, 2+ devices)", f"{k['storms']:,}", f"{k['storm_compression']} alerts → 1", delta_color="off")
c = st.columns(3)
c[0].metric("Team hand-offs avoided", f"{k['teams_avoided']:,}")
c[1].metric("Manual merges also grouped", f"{k['sn_merged_grouped_pct']}%", f"of {k['sn_merged']:,} merged in Msolve", delta_color="off")
c[2].metric("User tickets linked", f"{k['user_tickets_attached']:,}", f"of {k['user_tickets']:,}", delta_color="off")

tab_inc, tab_over, tab_gaps, tab_how = st.tabs(["Incidents", "Overview", "Data gaps", "How it works"])

# ------------------------------------------------------------------ incidents
with tab_inc:
    view = inc[(inc["members"] >= min_members) & (inc["start"].dt.date >= d0) & (inc["start"].dt.date <= d1)]
    if pick_plants:
        view = view[view["site"].isin(pick_plants)]
    view = view.sort_values("members", ascending=False)
    st.write(f"**{len(view):,} incidents** match the filters.")
    if view.empty:
        st.info("No incidents match. Lower 'at least N alerts' or widen the dates.")
    else:
        table = view[["incident_id", "site", "city", "start", "members", "devices", "layers", "parent_team", "child_teams",
                      "user_tickets", "confidence"]].copy()
        table["child_teams"] = table["child_teams"].apply(join)
        st.dataframe(table, width="stretch", hide_index=True, height=260)

        labels = {row.incident_id: f"{row.incident_id} · {row.site or row.city or 'no plant'} · {row.start:%d-%b %H:%M} · {row.members} alerts"
                  for row in view.head(300).itertuples()}
        sel = st.selectbox("Open an incident", list(labels), format_func=labels.get)
        i = inc.set_index("incident_id").loc[sel]
        mem = ev[ev["incident_id"] == sel].sort_values("created")
        users = ev[ev["attached_to"] == sel] if "attached_to" in ev else ev.iloc[0:0]

        left, right = st.columns([2, 3])
        with left:
            st.subheader(f"{sel}")
            st.markdown(f"**Plant:** {i['site'] or '–'} ({i['city'] or 'city unknown'})  \n"
                        f"**Time:** {i['start']:%d %b %H:%M} → {i['last_alert']:%H:%M} · **{i['members']} alerts** from {i['devices']} devices")
            st.markdown(f"🟧 **Primary problem** ({i['root_layer']})  \n`{i['root_title']}`")
            st.markdown(f"🟦 **Parent team:** **{i['parent_team']}** · confidence: {i['confidence']}")
            st.markdown("⬜ **Child teams (notified only):** " + (join(i["child_teams"]) or "none"))
            st.markdown(f"**Teams involved today:** {join(i['teams_today'])} → would be **1** owner")
            st.caption(f"Why grouped: {i['why']}")
            st.caption(f"Repeats: {i['repeats']} · self-healed: {i['self_healed']} · sources: {i['sources']}")
        with right:
            mem_plot = mem.assign(role=["primary problem" if e == i["root_event_id"] else "member" for e in mem["event_id"]],
                                  short=mem["title"].str.slice(0, 90))
            order = mem_plot.sort_values("layer_rank")["layer"].drop_duplicates().tolist()
            fig = px.strip(mem_plot, x="created", y="layer", color="role", hover_data={"short": True, "device": True, "created": "|%H:%M:%S"},
                           category_orders={"layer": order}, color_discrete_map={"primary problem": ORANGE, "member": BLUE})
            fig.update_traces(marker=dict(size=11, opacity=0.8))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title="Alert timeline by layer (top = most upstream)",
                              legend_title_text="", xaxis_title="", yaxis_title="")
            st.plotly_chart(fig, width="stretch")

        st.markdown("**Alerts and tickets in this incident**")
        st.dataframe(mem[["created", "event_id", "source", "kind", "device", "layer", "team_today", "closed_by", "self_healed", "repeat_of", "title"]],
                     width="stretch", hide_index=True, height=260)
        if len(users):
            st.markdown("**User tickets linked to this incident** (link ③: same city + time + outage words)")
            st.dataframe(users[["created", "event_id", "contact_type", "location", "title"]], width="stretch", hide_index=True)

# ------------------------------------------------------------------ overview
with tab_over:
    infra = ev.dropna(subset=["incident_id"])
    infra = infra[(infra["created"].dt.date >= d0) & (infra["created"].dt.date <= d1)]
    day = infra.groupby(infra["created"].dt.date).agg(alerts=("event_id", "size"), incidents=("incident_id", "nunique")).reset_index()
    day = day.melt("created", var_name="series", value_name="count")
    f1 = px.bar(day, x="created", y="count", color="series", barmode="group", color_discrete_map={"alerts": GREY, "incidents": BLUE},
                title="Per day: alerts & tickets today vs incidents with correlation")
    f1.update_layout(height=340, xaxis_title="", yaxis_title="", legend_title_text="")
    st.plotly_chart(f1, width="stretch")

    a, b = st.columns(2)
    multi = inc[(inc["members"] >= 2) & (inc["start"].dt.date >= d0) & (inc["start"].dt.date <= d1)]
    storms = multi[(multi["members"] >= 5) & (multi["devices"] >= 2)]
    top = storms.groupby(storms["site"].fillna(storms["city"]).fillna("no plant")).size().sort_values().tail(15).reset_index(name="storms")
    top.columns = ["plant", "storms"]
    f2 = px.bar(top, x="storms", y="plant", orientation="h", title="Plants with the most storms", color_discrete_sequence=[ORANGE])
    f2.update_layout(height=420, xaxis_title="", yaxis_title="")
    a.plotly_chart(f2, width="stretch")
    pt = multi["parent_team"].value_counts().reset_index()
    pt.columns = ["parent team", "incidents"]
    f3 = px.bar(pt, x="incidents", y="parent team", orientation="h", title="Who would own the grouped incidents", color_discrete_sequence=[BLUE])
    f3.update_layout(height=420, xaxis_title="", yaxis_title="", yaxis={"categoryorder": "total ascending"})
    b.plotly_chart(f3, width="stretch")

    rl = multi["root_layer"].value_counts().reset_index()
    rl.columns = ["layer of the primary problem", "incidents"]
    st.dataframe(rl, hide_index=True)

# ------------------------------------------------------------------ data gaps
with tab_gaps:
    infra_all = ev.dropna(subset=["incident_id"])
    no_dev = infra_all[infra_all["site"].isna() & ~infra_all["device_known"]]
    st.markdown(f"""
- **{k['site_coverage_pct']}%** of alerts can be placed at a plant and **{k['device_coverage_pct']}%** at a plant or a named device.
  **{len(no_dev):,} alerts** name neither (mostly FortiGate interface alerts) and stay ungrouped. The Zabbix host list fixes this.
- **SN ↔ AN copies linked by `correlation_id`:** {k['sn_an_links']:,} (link ①, removes double counting only).
- **Service requests excluded** from correlation: {k['requests_excluded']:,}.
- **Time zones:** Alfa *Closed* sheet converted from UTC to IST; Open/Resolved sheets and Msolve are IST.
- **Plant names** come from a starter site master (`poc/site_master.csv`) with guessed cities. Confirm it with the network team.
- **No topology yet:** parent/child uses a fixed layer order (power → network edge → network → virtualization/storage → server → database → application).
""")
    sm = load_site_master()
    found = infra_all["site"].value_counts()
    missing = found[~found.index.isin(sm["code"])]
    if len(missing):
        st.markdown("**Plant codes seen in alerts but missing from the site master:**")
        st.dataframe(missing.rename("alerts").reset_index().rename(columns={"site": "code"}), hide_index=True)
    st.markdown("**Most common alerts that can't be placed (need host names):**")
    st.dataframe(no_dev["signature"].value_counts().head(10).rename("alerts").reset_index(), hide_index=True, width="stretch")

# ------------------------------------------------------------------ how it works
with tab_how:
    st.markdown("""
**Three kinds of links**

| Link | Example | Key |
|---|---|---|
| ① SN ticket ↔ its AN copy | INC3598326 ↔ IMSN9603183 | `correlation_id` (exact; removes copies) |
| ② Infra alert ↔ infra alert | FortiGate down ↔ switch unreachable ↔ AP reboot, same plant | plant + time window + layer order |
| ③ User ticket ↔ infra incident | "Unable to access SAP" at Nagpur ↔ Nagpur network incident | city + time + outage words + matching layer |

**Pipeline**
1. **Load & normalise:** Alfa (3 sheets) and Msolve; convert to IST; reopened tickets kept once.
2. **Link ①:** drop AN copies of SN tickets.
3. **Enrich:** plant code, city, device, layer, symptom strength, team.
4. **Reduce noise:** *repeats* (same device + same problem within the repeat window) and *self-healed* (auto-closed by System quickly).
5. **Group (link ②):** same plant (or same device) with each alert within the group window of the previous one.
6. **Primary problem:** most upstream layer, then strongest symptom, then earliest.
7. **Teams:** parent = team of the primary problem's layer; child teams = teams of the other layers (notified, no ticket).
8. **Attach users (link ③).**
9. **Score:** compression, storms, hand-offs avoided, agreement with manual merges in Msolve.

Shadow mode: the engine only recommends. Engineers confirm or reject, and that feedback tunes the rules.
""")
