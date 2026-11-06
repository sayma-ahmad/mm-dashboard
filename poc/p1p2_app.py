"""
P1/P2 alert-correlation demo: IMZA + ALZA tickets from Alfa, placed with the RVTool nomenclature.

    streamlit run poc/p1p2_app.py --server.port 8502

Four inputs per alert: location (RVTool), timestamp, text (layer + symptom), dependency (VM -> ESXi host).
Settings in the sidebar re-run the engine live.
"""
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poc import config as C                     # noqa: E402
from poc import p1p2_engine as E                # noqa: E402
from poc import chains as CH                    # noqa: E402

st.set_page_config(page_title="P1/P2 Alert Correlation", page_icon="🚨", layout="wide")
ORANGE, BLUE, GREY, GREEN, RED = "#e4572e", "#2a6fdb", "#9aa5b1", "#1a9c6b", "#c0392b"
P1P2 = {"P1 Critical": "P1", "P2 High": "P2"}


@st.cache_data(show_spinner="Loading the Alfa export (first time only, ~10 s)...")
def get_alfa():
    return E.load_alfa()


@st.cache_data(show_spinner="Building the RVTool nomenclature...")
def get_nom():
    return E.load_nomenclature()


@st.cache_data(show_spinner="Placing every P1/P2 alert at its location...")
def get_prepared(use_rv, priorities, twins, impact):
    return E.prepare(alfa=get_alfa(), nom=get_nom(), use_rv=use_rv, priorities=priorities, pair_twins=twins, with_impact=impact)


@st.cache_data(show_spinner="Correlating...")
def get_result(use_rv, priorities, window, dedupe, flap, level, twins, impact):
    return E.correlate(get_prepared(use_rv, priorities, twins, impact),
                       E.Settings(window, dedupe, flap, level, use_rv, priorities, twins, impact))


def join(v):
    return ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v))


# ------------------------------------------------------------------ sidebar
st.sidebar.header("Scope")
pri = st.sidebar.multiselect("Priorities", list(P1P2), default=list(P1P2), format_func=P1P2.get)
if not pri:
    st.sidebar.error("Pick at least one priority."); st.stop()
use_rv = st.sidebar.toggle("Use RVTool nomenclature", value=True,
                           help="Off = place alerts with site codes and city words only. Shows what the RVTool inventory adds.")
twins = st.sidebar.toggle("Pair unnamed FortiGate alerts with their named twin", value=True,
                          help="Zabbix raises most FortiGate link alerts twice, with and without the firewall name, about 7 s apart. "
                               "On = the unnamed one takes the named one's location and counts as a repeat of it.")
impact_on = st.sidebar.toggle("Attach Msolve user tickets & P3 DB alerts (impact layer)", value=True,
                              help="Msolve incidents raised by people (not Monitoring, not SOC cases) and P3 Zabbix database alerts are linked "
                                   "to the incident at the same city whose time window covers them. They show the effect; they never form incidents.")
st.sidebar.header("Correlation rules")
window = st.sidebar.slider("Group window (minutes between alerts)", 5, 60, 15, 5,
                           help="An alert joins a group if it arrives within this many minutes of the previous one at the same location.")
level = st.sidebar.radio("Group alerts by", ["location", "city"],
                         format_func={"location": "Location (e.g. NAGPUR 1)", "city": "City (e.g. Nagpur)"}.get)
dedupe = st.sidebar.slider("Repeat window (same device + same problem)", 0, 30, 10, 5)
flap = st.sidebar.slider("Self-healed if auto-closed within (minutes)", 0, 15, 5, 1)

try:
    r = get_result(use_rv, tuple(sorted(pri)), window, dedupe, flap, level, twins, impact_on)
except FileNotFoundError as e:
    st.error(f"Input file not found: {e.filename}. Put the exports in {C.INPUT_DIR} or change FILES in poc/config.py.")
    st.stop()
k, inc, ev, nom = r["kpis"], r["incidents"], r["events"], get_nom()
alerts = ev[ev["kind"].eq("alert")]

st.sidebar.header("Filters")
dmin, dmax = ev["created"].min().date(), ev["created"].max().date()
start_default = max(dmin, pd.Timestamp("2026-10-01").date())
dates = st.sidebar.date_input("Dates", (start_default, dmax), min_value=dmin, max_value=dmax)
d0, d1 = (dates if isinstance(dates, tuple) and len(dates) == 2 else (start_default, dmax))
locs = sorted(inc["location"].dropna().unique())
pick_locs = st.sidebar.multiselect("Locations", locs)
min_members = st.sidebar.slider("Show incidents with at least N alerts", 1, 50, 5)

# ------------------------------------------------------------------ header + KPIs
st.title("P1/P2 Alert Correlation")
st.caption("Zabbix tickets (IMZA + ALZA) from Alfa, 1–14 Oct 2026 · location from the RVTool inventory · "
           "grouped by location and time · primary problem by dependency and layer → one parent team · "
           "all times IST (the Alfa Closed sheet is exported in UTC, the 'Z' timestamps, and converted +5:30)")

c = st.columns(4)
c[0].metric("P1/P2 tickets in", f"{k['tickets_in']:,}", f"P1 {k['p1']:,} · P2 {k['p2']:,}", delta_color="off")
c[1].metric("Recovery alerts removed", f"{k['recoveries']:,}", "'link is up' etc.", delta_color="off")
c[2].metric("Alerts with a location", f"{k['placed']:,}", f"{k['placed_pct']}% of {k['alerts']:,} · {k['fortigate_twins']:,} via twins", delta_color="off")
c[3].metric("Incidents out", f"{k['incidents_out']:,}", f"-{k['reduction_pct']}% vs placed alerts", delta_color="inverse")
c = st.columns(4)
c[0].metric("Storms (5+ alerts, 2+ devices)", f"{k['storms']:,}", f"{k['storm_compression']} alerts → 1", delta_color="off")
c[1].metric("Team hand-offs avoided", f"{k['teams_avoided']:,}")
c[2].metric("User tickets linked", f"{k['user_tickets_attached']:,}",
            f"of {k['user_tickets']:,} Msolve user tickets · DB alerts {k['db_alerts_attached']} of {k['db_alerts']}", delta_color="off")
c[3].metric("RVTool dependency links", f"{k['dependency_links']:,}",
            f"VM ↔ ESXi host in {k['dependency_incidents']} incident(s) · host is the primary in {k['dependency_as_root']}", delta_color="off")

tab_inc, tab_chain, tab_loc, tab_gaps, tab_how = st.tabs(["Incidents", "Chain window test", "Locations & nomenclature", "Data gaps", "How it works"])

# ------------------------------------------------------------------ incidents
with tab_inc:
    view = inc[(inc["members"] >= min_members) & (inc["start"].dt.date >= d0) & (inc["start"].dt.date <= d1)]
    if pick_locs:
        view = view[view["location"].isin(pick_locs)]
    view = view.sort_values("members", ascending=False)
    st.write(f"**{len(view):,} incidents** match the filters.")
    if view.empty:
        st.info("No incidents match. Lower 'at least N alerts' or widen the dates.")
    else:
        table = view[["incident_id", "root_event_id", "first_event_id", "location", "start", "members", "p1", "devices", "layers", "root_layer",
                      "parent_team", "child_teams", "impact", "dep_links", "confidence"]].copy()
        table.insert(6, "child alerts", table["members"] - 1)
        table["child_teams"] = table["child_teams"].apply(join)
        table = table.rename(columns={"incident_id": "incident (group)", "root_event_id": "parent (most likely cause)",
                                      "first_event_id": "first ticket raised", "members": "alerts in group", "impact": "user tickets / DB alerts",
                                      "p1": "P1 alerts", "root_layer": "parent layer", "dep_links": "RVTool links"})
        st.caption("One row = one incident (a group of alerts). **parent** = the ticket chosen as the most likely cause (most upstream layer). "
                   "**first ticket raised** = the one Zabbix noticed first; it is often a downstream symptom, because each Zabbix check "
                   "has its own interval. Open an incident below to see all its tickets.")
        grid = st.dataframe(table, width="stretch", hide_index=True, height=260, on_select="rerun", selection_mode="single-row")

        labels = {row.incident_id: f"{row.incident_id} · parent {row.root_event_id} · {row.location} · {row.start:%d-%b %H:%M} · {row.members} alerts"
                  for row in view.itertuples()}
        clicked = grid.selection.rows if grid and getattr(grid, "selection", None) else []
        start_at = list(labels).index(view.iloc[clicked[0]]["incident_id"]) if clicked else 0
        sel = st.selectbox("Open an incident (click a row above, or pick here)", list(labels), index=start_at, format_func=labels.get)
        i = inc.set_index("incident_id").loc[sel]
        mem = ev[ev["incident_id"] == sel].sort_values("created")
        mem = mem.assign(role=["PARENT (primary)" if e == i["root_event_id"] else "child" for e in mem["event_id"]])
        mem = mem.sort_values(["role", "created"])          # 'PARENT' sorts before 'child', so the parent is row 1
        # duplicate copies = unnamed FortiGate twins and same-check repeats; each points at its original through repeat_of.
        # They are real Alfa tickets, so they stay in the counts, but by default they fold into their original's row.
        is_copy = mem["repeat_of"].isin(mem["event_id"])
        copies = mem[is_copy].groupby("repeat_of")["event_id"].agg(list)
        n_copies = int(is_copy.sum())
        show_copies = st.checkbox("Show duplicate copies (unnamed FortiGate twins, same-check repeats) and repeat events of the same device "
                                  "as separate rows", value=False, key="show_copies")
        vis = mem if show_copies else mem[~is_copy]

        left, right = st.columns([2, 3])
        with left:
            st.subheader(sel)
            st.markdown(f"**Location:** {i['location']} ({i['city'] or 'city unknown'}) · placed by *{i['loc_method']}*, "
                        f"confidence {i['loc_confidence']}  \n"
                        f"**Time:** {i['start']:%d %b %H:%M} → {i['last_alert']:%H:%M} · **{i['members']} alerts** "
                        f"({i['p1']} P1) from {i['devices']} devices")
            st.markdown(f"🟧 **Parent = most likely cause** ({i['root_layer']}, {i['root_priority']}, raised {i['root_created']:%H:%M:%S})  \n`{i['root_title']}`")
            if i["first_event_id"] != i["root_event_id"]:
                st.markdown(f"⏱️ **First ticket raised:** `{i['first_event_id']}` at {i['start']:%H:%M:%S} ({i['first_layer']}), "
                            f"{(i['root_created'] - i['start']).total_seconds() / 60:.1f} min before the parent. Zabbix noticed this symptom first; "
                            f"the parent is chosen by layer (what sits upstream), not by who alerted first.")
            st.markdown(f"🟦 **Parent team:** **{i['parent_team']}** · confidence: {i['confidence']}")
            st.markdown("⬜ **Child teams (notified only):** " + (join(i["child_teams"]) or "none"))
            st.markdown(f"**Teams that got tickets today:** {join(i['teams_today'])} → would be **1** owner")
            st.caption(f"Why: {i['why']}")
            st.caption(f"Repeats: {i['repeats']} · self-healed: {i['self_healed']}")
        with right:
            mem_plot = vis.assign(role=["primary problem" if e == i["root_event_id"] else "member" for e in vis["event_id"]],
                                  short=vis["title"].str.slice(0, 90))
            order = mem_plot.sort_values("layer_rank")["layer"].drop_duplicates().tolist()
            fig = px.strip(mem_plot, x="created", y="layer", color="role", hover_data={"short": True, "device": True, "priority": True,
                           "created": "|%H:%M:%S"}, category_orders={"layer": order},
                           color_discrete_map={"primary problem": ORANGE, "member": BLUE})
            fig.update_traces(marker=dict(size=11, opacity=0.8))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), title="Alert timeline by layer (top = most upstream)",
                              legend_title_text="", xaxis_title="", yaxis_title="")
            st.plotly_chart(fig, width="stretch")

        # ---- RVTool dependency panel
        host_key = i["dep_host"] if isinstance(i["dep_host"], str) else ""
        host = nom["esxi"].get(host_key)
        if host:
            if i["dep_applied"]:
                st.markdown(f"**Resource dependency (RVTool):** the primary problem is ESXi host `{host_key}` in cluster "
                            f"`{host['cluster']}` at {host['location']}. **{host['n_vms']} VMs run on it.**")
            else:
                st.markdown(f"**Resource dependency (RVTool):** ESXi host `{host_key}` (cluster `{host['cluster']}`, **{host['n_vms']} VMs**) "
                            f"also alerted, but **{i['network_alerts']} network/power alerts** at the same site put the cause upstream of it. "
                            f"The host and its VMs are downstream symptoms, not the parent.")
            alerted = set(mem.loc[mem["runs_on_alert"].eq(i["dep_root"]), "loc_key"])
            rows = []
            for vm in host["vms"].split(" | "):
                h = nom["hosts"].get(vm, {})
                rows.append({"VM": vm, "alerted in this incident": "✅ yes" if vm in alerted else "– not yet (at risk)",
                             "application": h.get("application", ""), "OS": h.get("os", ""), "IP": h.get("ip", "")})
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            on_host = mem[mem["rv_esxi"].ne("") & mem["rv_esxi"].notna()]
            if len(on_host):
                st.markdown("**Resource dependency (RVTool):** " + "; ".join(
                    f"`{r.loc_key}` runs on ESXi `{r.rv_esxi}` (cluster `{r.rv_cluster}`)" for r in on_host.drop_duplicates("loc_key").head(6).itertuples()))

        n_child = int(i["members"]) - 1
        mem = vis.copy().sort_values("created")
        mem["copies"] = mem["event_id"].map({k: len(v) for k, v in copies.items()}).fillna(0).astype(int)
        mem["copy tickets"] = mem["event_id"].map({k: ", ".join(v) for k, v in copies.items()}).fillna("")
        mem["interface"] = mem["title"].str.extract(r"Interface (\w+)\(", expand=False).fillna("")   # a firewall has several tunnels
        # likely trigger = the nearest EARLIER alert from a more upstream layer inside this incident (the parent is the start of
        # the episode, not the direct cause of every child; alerts are chained by the rolling window). Copies never count as a trigger.
        bytime = mem
        trig, gap = {}, {}
        for n, row in enumerate(bytime.itertuples()):
            prev = bytime.iloc[:n]
            prev = prev[prev["repeat_of"].isna()]
            cand = prev[prev["layer_rank"] < row.layer_rank]
            if len(cand):
                t = cand.iloc[-1]
                trig[row.event_id] = t["event_id"]
                gap[row.event_id] = round((row.created - t["created"]).total_seconds() / 60, 1)
        mem["likely trigger"] = mem["event_id"].map(trig).fillna("")
        mem["min after trigger"] = mem["event_id"].map(gap)

        # ---- repeat events: the same device + interface + check firing again later in this group, beyond the 10-min repeat window
        # (a tunnel flapping twice, a switch rebooting twice). New tickets, same pattern: by default they fold into the first time
        # it happened, and a note says whether the same devices alerted after each time.
        key = (mem["device"].astype("string").fillna("") + "|" + mem["interface"].astype("string") + "|" + mem["signature"].astype("string"))
        key = key.where(mem["device"].notna() & mem["repeat_of"].isna())        # copies and nameless tickets never fold
        sub = mem[key.notna()]
        g = sub.groupby(key.loc[sub.index])
        mem["occn"] = g.cumcount().add(1).reindex(mem.index).fillna(1).astype(int)
        mem["firstocc"] = g["event_id"].transform("first").reindex(mem.index)
        is_recur = (mem["occn"] > 1) & mem["event_id"].ne(i["root_event_id"])
        n_recur = int(is_recur.sum())
        rec = mem[is_recur]
        # plain dicts: an empty datetime Series cannot be used as a .map() mapper
        again_at = {k: ", ".join(s.dt.strftime("%H:%M:%S")) for k, s in rec.groupby("firstocc")["created"]}
        again_ids = {k: ", ".join(s) for k, s in rec.groupby("firstocc")["event_id"]}
        rec_copies = {k: int(s.sum()) for k, s in rec.groupby("firstocc")["copies"]}
        rec_copy_ids = {k: ", ".join(x for x in s if x) for k, s in rec.groupby("firstocc")["copy tickets"]}
        ordinal = lambda k: f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"
        label = {r.event_id: f"{r.firstocc} ({ordinal(r.occn)} time)" for r in rec.itertuples()}
        followers = mem[mem["likely trigger"].ne("")].groupby("likely trigger")["device"].agg(lambda s: sorted(set(s.dropna().astype(str))))
        notes = []
        by_id = mem.set_index("event_id")
        for first_id, grp in rec.groupby("firstocc", sort=False):
            head = by_id.loc[first_id]
            ids = [first_id] + grp["event_id"].tolist()
            check = head["title"].split(" - ")[-1].split(": ", 1)[-1][:70]
            what = f"`{head['device']}`" + (f" `{head['interface']}`" if head["interface"] else "")
            times = ", ".join(by_id.loc[ids, "created"].dt.strftime("%H:%M:%S"))
            f1 = followers.get(first_id, [])
            parts = [f"{what} · *{check}* happened **{len(ids)} times**: {times}."]
            if not f1 and not any(followers.get(x, []) for x in ids[1:]):
                parts.append("No other device alerted after any of them.")
            else:
                parts.append(f"After the 1st time, {len(f1)} device(s) alerted" + (f" ({', '.join(f1[:8])}{'…' if len(f1) > 8 else ''})" if f1 else "") + ".")
                for kth, x in enumerate(ids[1:], start=2):
                    fk = followers.get(x, [])
                    same = sorted(set(fk) & set(f1))
                    tail = f", **{len(same)} the same as the 1st time**" if fk and f1 else ""
                    parts.append(f"After the {ordinal(kth)} time, {len(fk)} device(s) alerted" + (f" ({', '.join(fk[:8])}{'…' if len(fk) > 8 else ''})" if fk else "") + f"{tail}.")
            notes.append(" ".join(parts))

        if show_copies:
            shown = mem.copy()
            shown.loc[is_recur, "role"] = [f"child · {ordinal(o)} time of {f}" for o, f in zip(rec["occn"], rec["firstocc"])]
        else:
            shown = mem[~is_recur].copy()
            shown["copies"] = shown["copies"] + shown["event_id"].map(rec_copies).fillna(0).astype(int)
            extra = shown["event_id"].map(rec_copy_ids).fillna("")
            shown["copy tickets"] = [", ".join(x for x in (a, b) if x) for a, b in zip(shown["copy tickets"], extra)]
            shown["likely trigger"] = shown["likely trigger"].map(lambda t: label.get(t, t))
        shown["again at"] = shown["event_id"].map(again_at).fillna("")
        shown["again tickets"] = shown["event_id"].map(again_ids).fillna("")
        shown = shown.sort_values(["role", "created"])
        shown["raised #"] = shown["created"].rank(method="first").astype(int)

        bits = []
        if not show_copies and n_copies:
            bits.append(f"**{n_copies} duplicate {'copies' if n_copies != 1 else 'copy'}** folded into their original (column `copies`)")
        if not show_copies and n_recur:
            bits.append(f"**{n_recur} repeat {'events' if n_recur != 1 else 'event'}** of the same device and check folded into the first time it happened (column `again at`)")
        folded = (" · " + "; ".join(bits) + " · tick the box above to list them") if bits else ""
        st.markdown(f"**Tickets in this incident: parent `{i['root_event_id']}` + {n_child} child ticket(s)**{folded} · "
                    f"`raised #` = order in which Zabbix raised the tickets shown (1 = first); times are IST")
        duration = (i["last_alert"] - i["start"]).total_seconds() / 60
        if duration > 2 * window:
            st.info(f"This incident is a **{duration:.0f}-minute episode**: each alert arrived within {window} min of the previous one, so the "
                    f"chain never broke. The parent is the start of the episode, not the direct cause of every child; use **likely trigger** "
                    f"(the nearest earlier alert from a more upstream layer) to see what each ticket most likely follows from.")
        mem_out = shown[["raised #", "role", "event_id", "copies", "again at", "priority", "created", "device", "interface", "layer", "likely trigger",
                         "min after trigger", "team", "loc_method", "rv_esxi", "closed_by", "self_healed", "repeat_of", "copy tickets",
                         "again tickets", "title"]].rename(columns={"event_id": "ticket", "rv_esxi": "runs on ESXi"})
        st.dataframe(mem_out, width="stretch", hide_index=True, height=300)
        st.download_button("Download parent + child tickets (CSV)", mem_out.to_csv(index=False), file_name=f"{sel}_tickets.csv", mime="text/csv")
        if notes:
            with st.expander(f"Repeated events in this incident ({len(notes)}): the same device and check happened more than once", expanded=True):
                for line in notes:
                    st.markdown("- " + line)

        il = r.get("impact_links")
        if il is not None and len(il):
            mine = il[il["incident_id"].eq(sel)].drop(columns=["kind"]).merge(r["impact"], on="impact_id").sort_values("created")
            if len(mine):
                st.markdown(f"**Impact linked to this incident ({len(mine)}): Msolve user tickets and P3 database alerts** "
                            f"· link ③: same city, inside the time window, outage words, symptom fits a layer of the incident")
                st.dataframe(mine[["created", "impact_id", "kind", "confidence", "contact_type", "raw_location", "priority", "team", "minutes_from_start",
                                   "title", "reason"]], width="stretch", hide_index=True)
                st.caption("confidence: high = incident has 2+ alerts and the ticket is within 15 min of its start · medium = one of the two · low = neither")
            else:
                st.caption("No Msolve user ticket or DB alert links to this incident (same city, time window, outage words).")

# ------------------------------------------------------------------ chain window test
with tab_chain:
    st.markdown("""
**Does a network failure show up as one ticket per downstream layer at the same site, and how long after?**
A *chain* is a run of alerts at one location, each within the window of the previous one, with **two or more different layers**
(network edge → network → virtualization → server → database → application). Its *spine* is the first alert of each layer.
Repeats and FortiGate twins are left out, so a chain holds one ticket per device per layer. Move the window to see where chains appear.
""")
    with_imp = st.checkbox("Include the linked Msolve user tickets and DB alerts as the application / database layer", value=True,
                           disabled=r.get("impact_links") is None)
    ca = CH.spine_alerts(ev, r["impact"], r["impact_links"], inc) if (with_imp and r.get("impact_links") is not None) else CH.spine_alerts(ev)
    ca = ca[(ca["created"].dt.date >= d0) & (ca["created"].dt.date <= d1)]
    if pick_locs:
        ca = ca[ca["location"].isin(pick_locs)]
    lay_counts = ca["layer"].value_counts()
    st.caption("Chain-eligible alerts by layer: " + " · ".join(f"{l} {n:,}" for l, n in lay_counts.items())
               + ". Zabbix raises no application alerts at all, so the application layer here is the users' own tickets, linked by city and time.")
    cw = st.slider("Chain window: max seconds between one alert and the next", 30, 900, 120, 15, key="chain_window")
    ch, cmem = CH.chains_for_window(ca, cw)
    c = st.columns(4)
    c[0].metric("Chains (2+ layers)", f"{len(ch):,}")
    c[1].metric("Chains (3+ layers)", f"{int((ch['n_layers'] >= 3).sum()):,}")
    c[2].metric("Alerts inside chains", f"{int(ch['alerts'].sum()):,}", f"of {len(ca):,} chain-eligible alerts", delta_color="off")
    c[3].metric("Upstream layer detected first", f"{100 * ch['upstream_detected_first'].mean():.0f}%" if len(ch) else "–",
                "each Zabbix check has its own interval", delta_color="off")

    left, right = st.columns(2)
    sw = CH.window_sweep(ca)
    f1 = px.bar(sw.melt("window_s", value_vars=["chains (2+ layers)", "chains (3+ layers)"], var_name="series", value_name="chains"),
                x="window_s", y="chains", color="series", barmode="group", color_discrete_map={"chains (2+ layers)": BLUE, "chains (3+ layers)": ORANGE},
                title="Chains found vs window gap (seconds)")
    f1.update_layout(height=340, xaxis_title="window (s)", yaxis_title="", legend_title_text="", xaxis_type="category")
    left.plotly_chart(f1, width="stretch")
    gaps = CH.spine_gaps(ca)
    if len(gaps):
        f2 = px.histogram(gaps.assign(gap_s=gaps["gap_s"].clip(upper=1200)), x="gap_s", nbins=40, range_x=[0, 1200],
                          title="Seconds until the next layer appears (chains at a 15 min window; 1200 = 20 min or more)",
                          color_discrete_sequence=[GREY])
        for x, lbl in ((45, "45 s"), (120, "2 min"), (300, "5 min")):
            f2.add_vline(x=x, line_dash="dash", line_color=ORANGE, annotation_text=lbl, annotation_position="top")
        f2.update_layout(height=340, xaxis_title="gap (s)", yaxis_title="transitions")
        right.plotly_chart(f2, width="stretch")
        q = gaps["gap_s"].quantile([.25, .5, .75]).round(0)
        st.caption(f"{len(gaps)} layer-to-layer transitions · median gap {q[.5]:.0f} s · a quarter within {q[.25]:.0f} s · three quarters within {q[.75]:.0f} s · "
                   + " · ".join(f"≤{w} s: {100 * (gaps['gap_s'] <= w).mean():.0f}%" for w in (45, 60, 120, 300)))
        tr = gaps.groupby(["from_layer", "to_layer"]).agg(transitions=("gap_s", "size"), median_gap_s=("gap_s", "median")).round(0)
        tr = tr.sort_values("transitions", ascending=False).reset_index()
        st.markdown("**Which layer follows which (detection order) and the typical gap**")
        st.dataframe(tr, hide_index=True, width="stretch", height=240)

    st.markdown(f"**Chains at {cw} s**")
    if ch.empty:
        st.info("No chain at this window. Widen it.")
    else:
        st.dataframe(ch[["chain", "location", "start", "alerts", "devices", "n_layers", "spine", "span_s", "first_detected", "most_upstream"]],
                     hide_index=True, width="stretch", height=260)
        labels = {row.chain: f"chain {row.chain} · {row.location} · {row.start:%d-%b %H:%M} · {row.spine}" for row in ch.head(300).itertuples()}
        pick = st.selectbox("Open a chain", list(labels), format_func=labels.get, key="chain_pick")
        cm = CH.chain_members(ca, cmem, pick)
        cm["spine"] = cm["spine"].map({True: "★ spine", False: ""})
        st.dataframe(cm[["created", "gap_s", "spine", "event_id", "priority", "layer", "device", "team", "title"]], hide_index=True, width="stretch", height=300)

    x = CH.cross_location_test(ca, cw)
    st.markdown(f"""
**Cross-location test at {cw} s:** of **{x['plant_chains']}** plant chains, **{x['with_dc_alert_in_window']}** have a data-centre alert
(MDE, MDE-COLO, MRV-COLO, MRV-CHENNAI) inside the window. Shifting the data-centre alerts by 6 hours still gives **{x['baseline_shifted_6h']}**,
which is what coincidence alone produces. The difference is the real cross-site signal, and it is small: linking a plant alert to a
data-centre VM or database needs the dependency map (which plant uses which server), not a time window.
""")

# ------------------------------------------------------------------ locations
with tab_loc:
    fl = alerts[(alerts["created"].dt.date >= d0) & (alerts["created"].dt.date <= d1)]
    a, b = st.columns(2)
    top = fl.dropna(subset=["location"]).groupby(["location", "priority"]).size().reset_index(name="alerts")
    order = top.groupby("location")["alerts"].sum().sort_values().tail(20).index.tolist()
    f1 = px.bar(top[top["location"].isin(order)], x="alerts", y="location", color="priority", orientation="h",
                category_orders={"location": order}, color_discrete_map={"P1": RED, "P2": ORANGE}, title="P1/P2 alerts per location (top 20)")
    f1.update_layout(height=520, xaxis_title="", yaxis_title="", legend_title_text="")
    a.plotly_chart(f1, width="stretch")

    meth = fl["loc_method"].value_counts().reindex(E.LADDER).dropna().reset_index()
    meth.columns = ["how the location was found", "alerts"]
    f2 = px.bar(meth, x="alerts", y="how the location was found", orientation="h", title="Location ladder: first rule that matched",
                color="how the location was found",
                color_discrete_map={m: (GREEN if m.startswith("rv") else GREY if m == "unplaced" else ORANGE if m == "fortigate twin" else BLUE) for m in E.LADDER})
    f2.update_layout(height=520, xaxis_title="", yaxis_title="", showlegend=False, yaxis={"categoryorder": "array", "categoryarray": E.LADDER[::-1]})
    b.plotly_chart(f2, width="stretch")

    day = alerts.groupby(alerts["created"].dt.date).agg(alerts=("event_id", "size"), placed=("location", lambda s: 100 * s.notna().mean())).reset_index()
    day = day[day["alerts"] >= 100]
    f3 = px.line(day, x="created", y="placed", markers=True, title="% of alerts with a location, per day (jump = FortiGate alerts started naming the device)")
    f3.update_layout(height=300, xaxis_title="", yaxis_title="% placed", yaxis_range=[0, 100])
    st.plotly_chart(f3, width="stretch")

    st.markdown("**Alert site codes → RVTool location** (`poc/nomenclature/site_codes.csv`). Rows marked *guess* or *medium* need the network team to confirm.")
    sc = nom["site_codes"].copy()
    sc["alerts"] = sc["code"].map(alerts.loc[alerts["loc_method"].str.startswith("site code"), "loc_key"].value_counts()).fillna(0).astype(int)
    st.dataframe(sc[["code", "location", "city", "confidence", "alerts", "note"]].sort_values("alerts", ascending=False), hide_index=True, width="stretch")
    st.markdown(f"**RVTool nomenclature built from the inventory:** {len(nom['hosts']):,} server names, {len(nom['esxi']):,} ESXi/AHV hosts, "
                f"{len(nom['pfx6']) + len(nom['pfx5']):,} name prefixes, {len(nom['subnets']):,} /24 subnets, {len(nom['locations'])} locations.")

# ------------------------------------------------------------------ data gaps
with tab_gaps:
    st.markdown(f"""
- **{k['unplaced']:,} of {k['alerts']:,} alerts ({100 - k['placed_pct']:.1f}%) have no location.** {k['unplaced_fortigate_no_device']:,} of them are
  FortiGate interface alerts with no firewall name (`FortiGate: Interface OVMUM_ILL23(): Link status is down`), almost all from 1–5 Oct.
  Since 6 Oct Zabbix raises the same event twice, once with the firewall name (`MM-NAGP1-EDG-SDW01 - FortiGate: ...`), so
  **{k['fortigate_twins']:,} unnamed alerts were paired with their named twin**, placed, and counted as repeats.
  **If every FortiGate alert carried the device name, {k['placeable_if_fortigate_named_pct']}% of P1/P2 alerts would have a location.**
  Ask: add the Zabbix host name to all alert subjects, and drop the duplicate trigger.
- **RVTool adds {k['rvtool_added']:,} placements** on top of site codes (server names, ESXi hosts, VM IPs, name prefixes, subnets), and the
  VM → ESXi host chain for the dependency rule. Switch the toggle in the sidebar to compare.
- **{k['recoveries']:,} recovery alerts** ("link status is up") are tickets, not problems. They should close the matching "down" alert instead.
- **{k['self_healed']:,} alerts self-healed** (auto-closed by System within {flap} min): candidates for a delay before a ticket is raised.
- **No topology yet:** parent/child still uses a fixed layer order (network edge → network → virtualization → server → database → application)
  except where RVTool proves a VM runs on the alerting ESXi host.
""")
    unm = alerts[alerts["loc_method"].eq("site code (unmapped)")]["loc_key"].value_counts()
    if len(unm):
        st.markdown("**Site codes seen in alerts but not in `site_codes.csv`:**")
        st.dataframe(unm.rename("alerts").reset_index().rename(columns={"loc_key": "code"}), hide_index=True)
    st.markdown("**Most common alerts that can't be placed:**")
    st.dataframe(alerts[alerts["location"].isna()]["signature"].value_counts().head(10).rename("alerts").reset_index(), hide_index=True, width="stretch")

# ------------------------------------------------------------------ how it works
with tab_how:
    st.markdown("""
**Scope:** Alfa tickets whose ID starts with `IMZA` or `ALZA` (Zabbix) and priority P1 or P2. These never reach Msolve, so there is no
`correlation_id` to use; everything comes from the alert subject, its time and the RVTool inventory.

**Four inputs per alert**

| Input | Where it comes from | Used for |
|---|---|---|
| Location | RVTool nomenclature ladder (below) | which alerts can belong together |
| Timestamp | Alfa *Created On* (Closed sheet converted from UTC to IST) | the rolling time window |
| Text | subject → layer (network edge, network, virtualization, server, app) + symptom strength | which alert is the primary problem |
| Dependency | RVTool: VM → ESXi host → cluster | a host whose VMs also alerted is the primary problem, unless the network at that site failed too |

**Location ladder** (first rule that matches wins)
1. `rv host`: a server name from RVTool appears in the subject → location, cluster, ESXi host, application
2. `rv esxi host`: the IP (or AHV name) is an ESXi/AHV host in RVTool → location, cluster, the VMs on it
3. `rv vm ip`: the IP is a VM's primary IP in RVTool
4. `site code`: `MM-<CODE>-...` or a known code (`NSKP1`, `ZHBFD`, `MASPL-CHK`...) → `site_codes.csv`
5. `rv name prefix`: first 5–6 letters of the device name match an RVTool naming prefix (`MMNGP2…` → NAGPUR 2)
6. `rv subnet`: the /24 of the IP belongs to one RVTool location
7. `city word`: a city name in the text (city-level only)
8. `fortigate twin`: an unnamed FortiGate alert takes the location of the named alert raised for the same interface within 20 s, and counts as a repeat of it

**Pipeline**
1. Load Alfa, keep P1/P2 IMZA/ALZA, flag recovery alerts ("link is up").
2. Place each alert with the ladder.
3. Enrich: layer, symptom strength, team, ESXi host the VM runs on.
4. Noise: *repeats* (same device + same problem within the repeat window) and *self-healed* (auto-closed by System quickly).
5. Group: same location, each alert within the group window of the previous one.
6. Primary problem: most upstream layer (power → network edge → network → virtualization → server → app), then strongest symptom
   (a core switch outranks access switches and APs), then earliest. The RVTool rule "ESXi host whose VMs alerted" decides only when
   nothing more upstream failed at the same site in the same window: "Hypervisor is down" means Zabbix got no answer, and when the
   site's switches went silent at the same moment, the network is the cause and the host is a symptom.
7. Teams: parent = team of the primary problem's layer; child teams = teams of the other layers (notified, no ticket).
8. Impact layer (link ③): Msolve incidents raised by people (not Monitoring, not SOC security cases) and P3 Zabbix database alerts are
   attached to the incident at the same city whose time window covers them (10 min before the start to 60 min after the last alert).
   A user ticket must describe an outage and its symptom must fit a layer of the incident. These never form incidents; they show the effect.
   Note: nothing in Alfa or Msolve monitors applications. The 560 "Solman" tickets are SOC security cases (72) and user requests (488),
   so the only application-layer signal is what users report.

Shadow mode: the engine only recommends. Engineers confirm or reject, and that feedback tunes the rules.
""")
