"""
P1/P2 infra-alert correlation engine: IMZA + ALZA tickets from Alfa only.

Why a separate engine: ops act on P1/P2, and Zabbix tickets (IMZA/ALZA) never reach Msolve, so
correlation needs exactly four things from each alert:
  location   - where (plant / data centre), via the RVTool nomenclature
  timestamp  - when (IST)
  text       - what failed and how badly (layer + symptom strength)
  dependency - what runs on what (VM -> ESXi host -> cluster, from RVTool)

Pipeline (one function each):
  1. load      - Alfa export, P1/P2 IMZA/ALZA only, recovery alerts flagged
  2. place     - location ladder: RVTool host -> ESXi host -> VM IP -> site code -> name prefix -> subnet -> city word
  3. enrich    - layer, strength, signature, team, dependency
  4. noise     - self-healed, repeats
  5. group     - same location within a rolling window
  6. root      - RVTool dependency first (host whose VMs also alerted), else most upstream layer, strongest, earliest
  7. score
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .engine import (normalise_alfa, normalise_sn, _read_csv, _device, _layer, _strength, _signature, _site_from_text,
                     _team_for_layer, _group_ids)


@dataclass
class Settings:
    window_min: int = 15            # alerts join a group if within N min of the previous one at the same location
    dedupe_min: int = 10            # same device + same problem within N min = repeat
    flap_min: int = 5               # auto-closed by System within N min = self-healed
    group_level: str = "location"   # "location" (RVTool site) or "city"
    use_rvtool: bool = True         # what-if: off = site codes + city words only, to show what RVTool adds
    priorities: tuple = ("P1 Critical", "P2 High")
    pair_twins: bool = True         # unnamed FortiGate alert = twin of the named alert raised seconds later for the same interface
    attach_impact: bool = True      # link Msolve user tickets and P3 database alerts (the impact layer) to incidents at the same city
    attach_before_min: int = 10     # an impact event may arrive up to N min before the incident starts...
    attach_after_min: int = 60      # ...or up to N min after its last alert


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TOKEN_RE = re.compile(r"[A-Z][A-Z0-9-]{3,}")
RECOVERY_RE = re.compile(C.RECOVERY_WORDS, re.I)
CITY_ALIASES = {"BANGLORE": "Bangalore", "BENGALURU": "Bangalore", "NASIK": "Nashik", "ZAHIRABAD": "Zaheerabad", "KANDIVLI": "Kandivali"}
LADDER = ["rv host", "rv esxi host", "rv vm ip", "site code", "site code (unmapped)", "rv name prefix", "rv subnet", "city word",
          "fortigate twin", "unplaced"]
RV_METHODS = {"rv host", "rv esxi host", "rv vm ip", "rv name prefix", "rv subnet"}
VIRT_RANK = next(rank for name, rank, _ in C.LAYER_RULES if name == "virtualization")
# Msolve caller locations use their own words ("Chennai MRV", "Nash", "MDE Kandivali"): extra words -> city
IMPACT_CITY_ALIASES = {"NASH": "Nashik", "NASIK": "Nashik", "MDE": "Kandivali", "GATEWAY": "Mumbai", "WORLI": "Mumbai", "SEWRI": "Mumbai",
                       "MRV": "Chennai", "MSPT": "Chennai", "MEML": "Bangalore", "BANGLORE": "Bangalore", "ZHB": "Zaheerabad"}
SECURITY_TEXT = re.compile(r"snow ticket id|case id|custom_|azure admin|cyberdefence|\bsiem\b|phishing", re.I)


# ======================================================================= 1. load
def load_alfa(path=None):
    path = Path(path or C.FILES["alfa"])
    engine = "pyxlsb" if path.suffix.lower() == ".xlsb" else None      # .xlsb = Excel binary export (Alfa_Now.xlsb)
    sheets = pd.read_excel(path, sheet_name=None, dtype=str, engine=engine)
    return pd.concat([df.assign(sheet=name.split(" ")[0]) for name, df in sheets.items()], ignore_index=True)


def load_nomenclature(folder=None):
    d = Path(folder or C.NOMENCLATURE_DIR)
    if not (d / "rv_hosts.csv").exists():
        from .build_nomenclature import build
        build(out_dir=d)
    read = lambda name: pd.read_csv(d / name, dtype=str).fillna("")  # noqa: E731
    sc = read("site_codes.csv")
    sc["code"] = sc["code"].str.upper().str.strip()
    hosts = read("rv_hosts.csv").drop_duplicates("host").set_index("host")
    esxi = read("rv_esxi.csv").drop_duplicates("esxi").set_index("esxi")
    pfx = read("rv_prefixes.csv")
    locs = read("rv_locations.csv")
    city_of = dict(zip(locs["location"], locs["city"]))
    city_of.update(dict(zip(sc["location"], sc["city"])))
    return {
        "site_codes": sc, "codes": sc.loc[sc["match"].eq("code"), "code"].tolist(),
        "prefixes": sc.loc[sc["match"].eq("prefix"), "code"].tolist(),
        "site_loc": dict(zip(sc["code"], sc["location"])),
        "hosts": hosts.to_dict("index"), "esxi": esxi.to_dict("index"),
        "vm_ip": {r["ip"]: r for r in hosts.reset_index().to_dict("records") if r["ip"]},
        "pfx6": {r["prefix"]: r["location"] for r in pfx.to_dict("records") if r["length"] == "6"},
        "pfx5": {r["prefix"]: r["location"] for r in pfx.to_dict("records") if r["length"] == "5"},
        "subnets": dict(zip(read("rv_subnets.csv")["net24"], read("rv_subnets.csv")["location"])),
        "city_of": city_of, "locations": locs,
        "city_words": {c.upper(): c for c in set(city_of.values()) if c} | CITY_ALIASES,
    }


# ======================================================================= 2. place
def _place_one(subject, device, nom, use_rv):
    """Returns (location, method, key, cluster, esxi_host, application)."""
    s = str(subject)
    up = s.upper()
    m = IP_RE.search(s)
    ip = m.group(0) if m else None
    tokens = [t.rstrip("-") for t in TOKEN_RE.findall(up)]
    if use_rv:
        for t in tokens:
            h = nom["hosts"].get(t)
            if h:
                return h["location"], "rv host", t, h["cluster"], h["esxi_host"], h["application"]
        for key in ([ip] if ip else []) + tokens:
            e = nom["esxi"].get(key)
            if e:
                return e["location"], "rv esxi host", key, e["cluster"], key, e["apps"]
        if ip and ip in nom["vm_ip"]:
            h = nom["vm_ip"][ip]
            return h["location"], "rv vm ip", h["host"], h["cluster"], h["esxi_host"], h["application"]
    site = _site_from_text(s, nom["codes"], nom["prefixes"])
    if site:
        loc = nom["site_loc"].get(site)
        return (loc, "site code", site, "", "", "") if loc else (site, "site code (unmapped)", site, "", "", "")
    if use_rv:
        tok = re.split(r"[-_.(]", device.upper())[0] if isinstance(device, str) else ""
        if tok and len(tok) >= 5:
            for n, table in ((6, nom["pfx6"]), (5, nom["pfx5"])):
                loc = table.get(tok[:n])
                if loc:
                    return loc, "rv name prefix", tok[:n], "", "", ""
        if ip:
            loc = nom["subnets"].get(ip.rsplit(".", 1)[0])
            if loc:
                return loc, "rv subnet", ip.rsplit(".", 1)[0] + ".x", "", "", ""
    for word, city in nom["city_words"].items():
        if re.search(rf"(?<![A-Z]){re.escape(word)}(?![A-Z])", up):
            return f"{city.upper()} (city only)", "city word", word, "", "", ""
    return None, "unplaced", "", "", "", ""


def place(ev, nom, use_rv=True):
    cols = ["location", "loc_method", "loc_key", "rv_cluster", "rv_esxi", "rv_app"]
    placed = pd.DataFrame([_place_one(s, d, nom, use_rv) for s, d in zip(ev["title"], ev["device"])], columns=cols, index=ev.index)
    ev = ev.drop(columns=[c for c in cols if c in ev]).join(placed)
    ev["city"] = ev["location"].map(nom["city_of"])
    city_only = ev["loc_method"].eq("city word")
    ev.loc[city_only, "city"] = ev.loc[city_only, "location"].str.replace(" (city only)", "", regex=False).str.title()
    ev["loc_confidence"] = np.select(
        [ev["loc_method"].isin(["rv host", "rv esxi host", "rv vm ip"]), ev["loc_method"].eq("site code"),
         ev["loc_method"].isin(["rv name prefix", "rv subnet"]), ev["loc_method"].eq("unplaced")],
        ["high", "high", "medium", "none"], "low")
    return ev


def _pair_fortigate_twins(ev, tol_s=C.FORTIGATE_TWIN_SECONDS):
    """Zabbix raises most FortiGate link alerts twice: 'FortiGate: Interface OVMUM_ILL21(): Link status is down' (no firewall
    name) and ~7 s later 'MM-NAGP1-EDG-SDW01 - FortiGate: Interface OVMUM_ILL21(): Link down'. The unnamed one is a twin of
    the named one: it gets the twin's location and device, and is marked as a repeat of it."""
    fg = ev["title"].str.contains("fortigate", case=False, na=False) & ev["kind"].eq("alert")
    iface = ev["title"].str.extract(r"Interface (\w+)\(", expand=False)
    cols = ["event_id", "created", "location", "city", "loc_key", "device"]
    named = ev.loc[fg & ev["device_known"], cols].assign(iface=iface).dropna(subset=["iface"]).sort_values("created")
    unnamed = ev.loc[fg & ~ev["device_known"], ["event_id", "created"]].assign(iface=iface).dropna(subset=["iface"]).sort_values("created")
    if named.empty or unnamed.empty:
        return ev
    pairs = pd.merge_asof(unnamed, named.rename(columns={"event_id": "twin", "created": "twin_created"}),
                          left_on="created", right_on="twin_created", by="iface", direction="nearest",
                          tolerance=pd.Timedelta(seconds=tol_s)).dropna(subset=["twin"]).set_index("event_id")
    idx = ev["event_id"].isin(pairs.index)
    hit = pairs.loc[ev.loc[idx, "event_id"]]
    for col in ("location", "city", "loc_key", "device"):
        ev.loc[idx, col] = hit[col].to_numpy()
    ev.loc[idx, "twin_of"] = hit["twin"].to_numpy()
    ev.loc[idx, "loc_method"] = "fortigate twin"
    ev.loc[idx, "loc_confidence"] = "medium"
    return ev


# ======================================================================= 3. enrich
def build_events(alfa, nom, priorities=C.P1P2_PRIORITIES, use_rv=True, pair_twins=True):
    a = normalise_alfa(alfa)
    a = a[a["prefix"].isin(C.P1P2_PREFIXES) & a["Priority"].isin(list(priorities))].copy()
    ev = pd.DataFrame({
        "event_id": a["Ticket ID"], "created": a["created"], "closed": a["closed"], "title": a["Subject"],
        "priority": a["Priority"].str.slice(0, 2), "prefix": a["prefix"], "ticket_type": a["Ticket Type"],
        "raw_team": a["Department"], "closed_by": a["Closed By"], "sheet": a["sheet"], "reopens": a["reopens"],
    })
    ev["kind"] = np.where(ev["title"].str.contains(RECOVERY_RE, na=False), "recovery", "alert")
    ev["device"] = ev["title"].map(_device)
    ev["device_known"] = ev["device"].notna() & ~ev["device"].astype("string").str.startswith("FortiGate ").fillna(False)
    ev = place(ev, nom, use_rv)
    ev["twin_of"] = None
    if pair_twins:
        ev = _pair_fortigate_twins(ev)
    lr = ev["title"].map(_layer)
    ev["layer"], ev["layer_rank"] = lr.str[0], lr.str[1]
    ev["strength"] = ev["title"].map(_strength)
    core = ev["title"].str.contains(C.CORE_DEVICE_WORDS, case=False, na=False) & ev["layer"].eq("network")
    ev.loc[core, "strength"] += 1           # a core / distribution switch outranks access switches and APs
    ev["signature"] = ev["title"].map(_signature)
    # a firewall has several ISP links (OVMUM_ILL12, ILL13, ILL22...): each link is its own problem, so keep the
    # interface name in the signature; only the SAME link dropping again within the repeat window is a repeat
    iface = ev["title"].str.extract(r"Interface (\w+)\(", expand=False)
    ev.loc[iface.notna(), "signature"] = ev.loc[iface.notna(), "signature"] + " [" + iface[iface.notna()] + "]"
    ev["team"] = [_team_for_layer(l, t) for l, t in zip(ev["layer"], ev["title"])]
    ev["minutes_open"] = (ev["closed"] - ev["created"]).dt.total_seconds() / 60
    ev["is_esxi_alert"] = ev["loc_method"].eq("rv esxi host")
    ev["fortigate_no_device"] = ev["title"].str.contains("fortigate", case=False, na=False) & ~ev["device_known"]
    return ev.sort_values("created").reset_index(drop=True)


# ======================================================================= 4. noise
def reduce_noise(ev, st: Settings):
    ev = ev.copy()
    ev["self_healed"] = ev["closed_by"].isin(C.AUTO_CLOSERS) & (ev["minutes_open"] <= st.flap_min)
    key = np.where(ev["device_known"], "dev:" + ev["device"].astype(str) + "|" + ev["signature"], None)
    last, out, win = {}, [], pd.Timedelta(minutes=st.dedupe_min)
    for k, eid, t in zip(key, ev["event_id"], ev["created"]):
        if k is None:
            out.append(None); continue
        first_id, last_t = last.get(k, (None, None))
        if first_id is not None and t - last_t <= win:
            out.append(first_id); last[k] = (first_id, t)
        else:
            out.append(None); last[k] = (eid, t)
    ev["repeat_of"] = [r if r is not None else (t if isinstance(t, str) else None) for r, t in zip(out, ev["twin_of"])]
    return ev


# ======================================================================= 5. group
def group_events(ev, st: Settings):
    ev = ev.copy()
    loc = ev["location"] if st.group_level == "location" else ev["city"].fillna(ev["location"])
    usable = ev["kind"].eq("alert") & loc.notna()
    ev["group_key"] = np.where(usable, "loc:" + loc.astype(str), None)
    ev["incident_id"] = None
    sub = ev[usable]
    if len(sub):
        gid = _group_ids(sub["group_key"].to_numpy().astype(str),
                         sub["created"].to_numpy().astype("datetime64[ns]").astype("int64"), st.window_min)
        ev.loc[usable, "incident_id"] = [f"P-{g + 1:05d}" for g in gid]
    return ev


# ======================================================================= 6. root & teams
def build_incidents(ev, nom):
    m = ev.dropna(subset=["incident_id"]).copy()
    m["is_repeat"] = m["repeat_of"].notna()
    # root candidates: most upstream layer, never a repeat / twin, strongest symptom, earliest
    m = m.sort_values(["incident_id", "layer_rank", "is_repeat", "strength", "created"], ascending=[True, True, True, False, True])
    # RVTool dependency: a VM alert whose ESXi host also alerted in the same incident
    esx = m[m["is_esxi_alert"]][["incident_id", "loc_key", "event_id", "created"]].rename(
        columns={"loc_key": "rv_esxi", "event_id": "host_alert", "created": "host_alert_time"})
    dep = m[~m["is_esxi_alert"] & m["rv_esxi"].ne("")][["incident_id", "event_id", "rv_esxi"]].merge(esx, on=["incident_id", "rv_esxi"])
    dep = dep.sort_values("host_alert_time").drop_duplicates("event_id")
    m["runs_on_alert"] = m["event_id"].map(dep.set_index("event_id")["host_alert"])
    dep_root = dep.groupby("incident_id").agg(dep_root=("host_alert", "first"), dep_host=("rv_esxi", "first"), dep_links=("event_id", "size"))

    roots = m.drop_duplicates("incident_id").set_index("incident_id")
    g = m.groupby("incident_id")
    by_rank = m.drop_duplicates(["incident_id", "layer"])
    inc = pd.DataFrame({
        "location": g["location"].first(), "city": g["city"].first(),
        "start": g["created"].min(), "last_alert": g["created"].max(), "end": g["closed"].max(),
        "members": g.size(), "p1": g["priority"].apply(lambda s: int((s == "P1").sum())),
        "repeats": g["is_repeat"].sum().astype(int), "self_healed": g["self_healed"].sum().astype(int),
        "devices": g["device"].nunique(), "layers": by_rank.groupby("incident_id")["layer"].agg(", ".join),
        "loc_confidence": g["loc_confidence"].agg(lambda s: s.value_counts().index[0]),
        "loc_method": g["loc_method"].agg(lambda s: s.value_counts().index[0]),
    })
    inc = inc.join(dep_root)
    inc["dep_links"] = inc["dep_links"].fillna(0).astype(int)
    inc["min_rank"] = g["layer_rank"].min()
    inc["network_alerts"] = g["layer"].apply(lambda s: int(s.isin(["power", "network edge", "network"]).sum()))
    # root = the ESXi host whose VMs alerted (RVTool) - but only when nothing more upstream (power, network) failed at the
    # same site in the same window: then the network is the primary and the host + its VMs are downstream of it.
    # Otherwise: most upstream layer, then strongest symptom, then earliest.
    inc["dep_applied"] = inc["dep_root"].notna() & (inc["min_rank"] >= VIRT_RANK)
    root_id = inc["dep_root"].where(inc["dep_applied"]).fillna(roots["event_id"])
    rt = m.drop_duplicates("event_id").set_index("event_id").loc[root_id]
    for col, src in (("root_event_id", None), ("root_title", "title"), ("root_layer", "layer"), ("root_device", "device"),
                     ("root_priority", "priority"), ("root_created", "created"), ("root_esxi", "loc_key")):
        inc[col] = root_id.to_numpy() if src is None else rt[src].to_numpy()
    inc["parent_team"] = rt["team"].to_numpy()
    first = m.sort_values("created").drop_duplicates("incident_id").set_index("incident_id")   # the ticket Zabbix raised first
    inc["first_event_id"] = first["event_id"]
    inc["first_layer"] = first["layer"]
    inc["root_is_earliest"] = inc["root_created"].eq(inc["start"])
    teams = m[["incident_id", "team"]].merge(inc[["parent_team"]], left_on="incident_id", right_index=True)
    teams = teams[teams["team"] != teams["parent_team"]].drop_duplicates().sort_values("team")
    inc["child_teams"] = teams.groupby("incident_id")["team"].agg(list)
    inc["child_teams"] = inc["child_teams"].apply(lambda v: v if isinstance(v, list) else [])
    all_teams = m.drop_duplicates(["incident_id", "team"]).groupby("incident_id")["team"].agg(list)
    inc["teams_today"] = all_teams
    inc["teams_avoided"] = (inc["teams_today"].apply(len) - 1).clip(lower=0)
    inc["tickets_avoided"] = inc["members"] - 1
    esxi_tab = nom["esxi"]
    inc["vms_on_root_host"] = [int(esxi_tab[k]["n_vms"]) if (k in esxi_tab) else 0 for k in inc["root_esxi"].fillna("")]
    inc["confidence"] = np.select(
        [inc["members"].eq(1), inc["dep_applied"], inc["loc_confidence"].eq("high") & inc["root_is_earliest"],
         inc["loc_confidence"].eq("high"), inc["loc_confidence"].eq("medium")],
        ["single", "high (dependency)", "high", "medium", "medium"], "low")
    place_ = inc["location"].fillna("unknown").astype(object)
    inc["why"] = [
        "single alert - nothing to group" if n == 1 else
        (f"ESXi host {h} alerted and {d} VM(s) on it alerted too (RVTool dependency); nothing more upstream failed" if applied else
         f"same location ({p}, placed by {meth}) · alerts within the window · primary = most upstream layer '{lay}'"
         + (f" ({net} network/power alerts)" if net else "") + (" and earliest" if e else "")
         + (f" · ESXi host {h} and {d} VM(s) on it also alerted: downstream of the network, not the cause (RVTool)" if d > 0 else ""))
        for n, h, d, applied, net, p, meth, lay, e in zip(inc["members"], inc["dep_host"].fillna(""), inc["dep_links"], inc["dep_applied"],
                                                          inc["network_alerts"], place_, inc["loc_method"], inc["root_layer"], inc["root_is_earliest"])]
    inc = inc.reset_index().sort_values("start").reset_index(drop=True)
    return inc, m[["event_id", "runs_on_alert"]].set_index("event_id")["runs_on_alert"]


# ======================================================================= 6b. impact layer: user tickets + database alerts
def load_sn(path=None):
    return normalise_sn(_read_csv(path or C.FILES["sn_incidents"]))


def _impact_city(text, nom):
    up = str(text).upper()
    words = {**nom["city_words"], **IMPACT_CITY_ALIASES}
    for word in sorted(words, key=len, reverse=True):
        if re.search(rf"(?<![A-Z]){re.escape(word)}(?![A-Z])", up):
            return words[word]
    return None


def build_impact_events(sn, alfa, nom, use_rv=True):
    """Everything that shows the *effect* of an infra incident but is not a P1/P2 Zabbix alert:
    - Msolve incidents raised by people (not Monitoring, not SOC security cases): kind 'user ticket', city from the caller location
    - P3 Zabbix database alerts from Alfa: kind 'db alert', placed with the location ladder"""
    u = sn[sn["contact_type"].ne("Monitoring").fillna(True) & ~sn["short_description"].str.contains(SECURITY_TEXT, na=False)].copy()
    text = (u["caller_id.location"].fillna("") + " | " + u["short_description"].fillna("")).str.lower()
    fits = [[layers for pat, layers in C.USER_SYMPTOM_LAYERS.items() if re.search(pat, t)] for t in text]
    users = pd.DataFrame({
        "impact_id": u["number"], "kind": "user ticket", "created": u["created"], "closed": u["closed"],
        "title": u["short_description"], "priority": u["priority"], "contact_type": u["contact_type"],
        "raw_location": u["caller_id.location"], "team": u["assignment_group"],
        "city": [_impact_city(t, nom) for t in text], "layer": "application",
        "symptom_layers": [sorted({l for f in ff for l in f}) for ff in fits],
        "outage": text.str.contains(C.GENERIC_OUTAGE_WORDS, na=False).to_numpy(),
    })
    a = normalise_alfa(alfa)
    a = a[a["prefix"].isin(C.P1P2_PREFIXES) & ~a["Priority"].isin(list(C.P1P2_PRIORITIES))]
    lay = a["Subject"].map(_layer)
    a = a[lay.str[0].isin(["database", "application"])].copy()
    a["layer"] = lay.str[0]
    dbs = pd.DataFrame({"impact_id": a["Ticket ID"], "kind": "db alert", "created": a["created"], "closed": a["closed"], "title": a["Subject"],
                        "priority": a["Priority"].str.slice(0, 2), "contact_type": "Zabbix", "raw_location": None, "team": a["Department"],
                        "layer": a["layer"], "symptom_layers": [[l] for l in a["layer"]], "outage": True})
    dbs["device"] = dbs["title"].map(_device)
    placed = pd.DataFrame([_place_one(s, d, nom, use_rv) for s, d in zip(dbs["title"], dbs["device"])],
                          columns=["location", "loc_method", "loc_key", "rv_cluster", "rv_esxi", "rv_app"], index=dbs.index)
    dbs["city"] = placed["location"].map(nom["city_of"])
    dbs.loc[placed["loc_method"].eq("city word"), "city"] = placed.loc[placed["loc_method"].eq("city word"), "location"].str.replace(" (city only)", "", regex=False).str.title()
    imp = pd.concat([users, dbs.drop(columns=["device"])], ignore_index=True)
    return imp.dropna(subset=["created"]).sort_values("created").reset_index(drop=True)


def attach_impact(inc, imp, st: Settings):
    """Link an impact event to the incident at the same city whose time window covers it. A user ticket must describe an
    outage and fit a layer of the incident; a database alert only needs the city and the time."""
    cand = inc[inc["city"].notna()][["incident_id", "city", "start", "last_alert", "layers", "location", "members"]]
    rows = []
    if len(imp) and len(cand):
        j = imp.dropna(subset=["city"]).merge(cand, on="city")
        j = j[(j["created"] >= j["start"] - pd.Timedelta(minutes=st.attach_before_min)) &
              (j["created"] <= j["last_alert"] + pd.Timedelta(minutes=st.attach_after_min))]
        for r in j.itertuples():
            inc_layers = str(r.layers).split(", ")
            fit = any(l in inc_layers for l in r.symptom_layers)
            if r.kind == "user ticket" and not (r.outage and fit):
                continue
            gap = (r.created - r.start).total_seconds() / 60
            why = (f"user reports an outage at {r.city} · symptom fits incident layers ({', '.join(l for l in r.symptom_layers if l in inc_layers)})"
                   if r.kind == "user ticket" else f"database server alert at {r.city} during the incident")
            close = abs(gap) <= 15                      # within 15 min of the incident start
            conf = "high" if (r.members >= 2 and close) else ("medium" if (r.members >= 2 or close) else "low")
            rows.append({"impact_id": r.impact_id, "incident_id": r.incident_id, "kind": r.kind, "minutes_from_start": round(gap, 1),
                         "confidence": conf, "reason": why, "abs_gap": abs(gap)})
    links = pd.DataFrame(rows, columns=["impact_id", "incident_id", "kind", "minutes_from_start", "confidence", "reason", "abs_gap"])
    if len(links):
        links = links.sort_values("abs_gap").drop_duplicates("impact_id").drop(columns="abs_gap")
    else:
        links = links.drop(columns="abs_gap")
    return links.reset_index(drop=True)


# ======================================================================= 7. score
def score(ev, inc, st: Settings, imp=None, links=None):
    alerts = ev[ev["kind"].eq("alert")]
    placed = alerts[alerts["location"].notna()]
    no_rv = alerts["loc_method"].isin(["site code", "site code (unmapped)", "city word", "fortigate twin"])
    multi = inc[inc["members"] >= 2]
    storms = inc[(inc["members"] >= 5) & (inc["devices"] >= 2)]
    return {
        "tickets_in": int(len(ev)), "p1": int((ev["priority"] == "P1").sum()), "p2": int((ev["priority"] == "P2").sum()),
        "recoveries": int((ev["kind"] == "recovery").sum()),
        "alerts": int(len(alerts)),
        "placed": int(len(placed)), "placed_pct": round(100 * len(placed) / max(len(alerts), 1), 1),
        "placed_pct_no_rvtool": round(100 * no_rv.mean(), 1) if len(alerts) else 0.0,
        "rvtool_added": int(alerts["loc_method"].isin(RV_METHODS).sum()),
        "fortigate_twins": int(alerts["twin_of"].notna().sum()),
        "unplaced": int(alerts["location"].isna().sum()),
        "unplaced_fortigate_no_device": int((alerts["location"].isna() & alerts["fortigate_no_device"]).sum()),
        "placeable_if_fortigate_named_pct": round(100 * (len(placed) + (alerts["location"].isna() & alerts["fortigate_no_device"]).sum())
                                                  / max(len(alerts), 1), 1),
        "incidents_out": int(len(inc)),
        "reduction_pct": round(100 * (1 - len(inc) / max(len(placed), 1)), 1),
        "multi_alert_incidents": int(len(multi)), "alerts_in_multi": int(multi["members"].sum()),
        "storms": int(len(storms)), "storm_compression": round(storms["members"].sum() / max(len(storms), 1), 1),
        "teams_avoided": int(inc["teams_avoided"].sum()),
        "self_healed": int(alerts["self_healed"].sum()), "repeats": int(alerts["repeat_of"].notna().sum()),
        "dependency_incidents": int((inc["dep_links"] > 0).sum()), "dependency_links": int(inc["dep_links"].sum()),
        "dependency_as_root": int(inc["dep_applied"].sum()),
        "by_method": alerts["loc_method"].value_counts().to_dict(),
        "user_tickets": int((imp["kind"] == "user ticket").sum()) if imp is not None else 0,
        "user_tickets_attached": int((links["kind"] == "user ticket").sum()) if links is not None else 0,
        "db_alerts": int((imp["kind"] == "db alert").sum()) if imp is not None else 0,
        "db_alerts_attached": int((links["kind"] == "db alert").sum()) if links is not None else 0,
        "incidents_with_impact": int(inc["impact"].gt(0).sum()) if "impact" in inc else 0,
        "settings": asdict(st),
    }


# ======================================================================= run
def prepare(alfa=None, nom=None, use_rv=True, priorities=C.P1P2_PRIORITIES, pair_twins=True, with_impact=True, sn=None):
    """Steps 1-3 (slow, ~15 s): cache this per (use_rvtool, priorities, pair_twins, with_impact)."""
    nom = nom or load_nomenclature()
    alfa = alfa if alfa is not None else load_alfa()
    out = {"events": build_events(alfa, nom, priorities, use_rv, pair_twins), "nom": nom, "impact": None}
    if with_impact:
        out["impact"] = build_impact_events(sn if sn is not None else load_sn(), alfa, nom, use_rv)
    return out


def correlate(prepared, st: Settings | None = None):
    """Steps 4-7 (fast): re-run whenever a slider moves."""
    st = st or Settings()
    ev = reduce_noise(prepared["events"], st)
    ev = group_events(ev, st)
    inc, runs_on = build_incidents(ev, prepared["nom"])
    ev["runs_on_alert"] = ev["event_id"].map(runs_on)
    imp = prepared.get("impact")
    links = attach_impact(inc, imp, st) if (st.attach_impact and imp is not None) else None
    if links is not None:
        inc["impact"] = inc["incident_id"].map(links.groupby("incident_id").size()).fillna(0).astype(int)
        inc["user_tickets"] = inc["incident_id"].map(links[links["kind"].eq("user ticket")].groupby("incident_id").size()).fillna(0).astype(int)
    else:
        inc["impact"], inc["user_tickets"] = 0, 0
    return {"events": ev, "incidents": inc, "impact": imp, "impact_links": links, "kpis": score(ev, inc, st, imp, links)}


def run(st: Settings | None = None):
    st = st or Settings()
    return correlate(prepare(use_rv=st.use_rvtool, priorities=st.priorities, pair_twins=st.pair_twins, with_impact=st.attach_impact), st)
