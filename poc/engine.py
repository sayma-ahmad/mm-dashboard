"""
Correlation engine for the Mahindra alert-correlation POC.

Pipeline (each step is one function below):
  1. load         - read Alfa (AN) and Msolve (SN) exports
  2. normalise    - one time zone (IST), one row per ticket, reopen counts
  3. link         - SN ticket <-> its AN copy via correlation_id   (link 1: same ticket)
  4. enrich       - plant/site, city, device, layer, symptom strength, team
  5. reduce noise - self-healed (auto-closed fast) and repeated alerts
  6. group        - same site (or device) within a rolling time window     (link 2: infra <-> infra)
  7. root & teams - primary problem, parent team, child teams
  8. attach users - user tickets to the incident at their city            (link 3: user <-> infra)
  9. score        - KPIs for the backtest
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from . import config as C


@dataclass
class Settings:
    window_min: int = 15                  # alerts join a group if within N min of the previous one
    group_level: str = "site"             # "site" (plant code) or "city"
    dedupe_min: int = 10                  # same device + same problem within N min = repeat
    text_fallback: bool = False           # alerts with no device: de-duplicate and group by identical text (riskier)
    flap_min: int = 5                     # auto-closed by System within N min = self-healed
    attach_before_min: int = 10           # user ticket may arrive up to N min before the incident starts...
    attach_after_min: int = 60            # ...or up to N min after its last alert


# ======================================================================= 1. load
def _read_csv(path):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Can't read {path}")


def _clean(s):
    return (s.astype("string").str.replace("_x000D_", "", regex=False)
            .str.replace(r"\s*[\r\n]+\s*", " | ", regex=True).str.strip())


def load_raw(files=None):
    files = files or C.FILES
    sheets = pd.read_excel(files["alfa"], sheet_name=None, dtype=str)
    alfa = pd.concat([df.assign(sheet=name.split(" ")[0]) for name, df in sheets.items()], ignore_index=True)
    sn = _read_csv(files["sn_incidents"])
    req = _read_csv(files["sn_requests"])
    return {"alfa": alfa, "sn": sn, "sn_requests": req}


# ======================================================================= 2. normalise
def _alfa_time(s, is_utc):
    t = pd.to_datetime(s.astype("string").str.replace("T", " ", regex=False)
                       .str.replace(r"\.\d+Z$", "", regex=True), errors="coerce")
    return t + pd.Timedelta(hours=C.UTC_TO_IST_HOURS) if is_utc else t


def _hhmm_hours(s):
    p = s.astype("string").str.extract(r"^(\d+):(\d+)$").astype(float)
    return p[0] + p[1] / 60


def normalise_alfa(alfa):
    a = alfa.copy()
    a["Subject"] = _clean(a["Subject"])
    utc = a["sheet"].isin(C.ALFA_UTC_SHEETS)
    a["created"] = pd.NaT
    a["closed"] = pd.NaT
    for is_utc in (True, False):
        m = utc == is_utc
        a.loc[m, "created"] = _alfa_time(a.loc[m, "Created On"], is_utc)
        if "Closed On" in a:
            a.loc[m, "closed"] = _alfa_time(a.loc[m, "Closed On"], is_utc)
    a["created"] = pd.to_datetime(a["created"])
    a["closed"] = pd.to_datetime(a["closed"])
    res = a["sheet"].eq("Resolved")
    a.loc[res, "closed"] = a.loc[res, "created"] + pd.to_timedelta(_hhmm_hours(a.loc[res, "ResolvedHours(HH:MM)"]), unit="h")
    a["prefix"] = a["Ticket ID"].str.extract(r"^([A-Za-z]+)", expand=False)
    a["num"] = a["Ticket ID"].str.extract(r"(\d{6,})", expand=False)
    # duplicates = reopened tickets: keep the current state (Open > Resolved > latest Closed), count reopens
    a["_rank"] = a["sheet"].map({"Open": 0, "Resolved": 1, "Closed": 2}).fillna(3)
    a = a.sort_values(["Ticket ID", "_rank", "closed"], ascending=[True, True, False])
    a["reopens"] = a.groupby("Ticket ID")["Ticket ID"].transform("size") - 1
    a = a.drop_duplicates("Ticket ID").drop(columns="_rank")
    return a.dropna(subset=["created"]).reset_index(drop=True)


def normalise_sn(sn):
    s = sn.copy()
    for col in s.columns:
        s[col] = _clean(s[col])
    s["created"] = pd.to_datetime(s["sys_created_on"], dayfirst=True, errors="coerce")
    s["closed"] = pd.to_datetime(s["resolved_at"], dayfirst=True, errors="coerce")
    s["num"] = s["correlation_id"].str.extract(r"(\d{6,})", expand=False)
    s["merged"] = s["close_notes"].str.contains("merged", case=False, na=False)
    return s.dropna(subset=["created"]).reset_index(drop=True)


# ======================================================================= 3. link SN <-> AN
def link_sn_an(a, s):
    copies = a[a["prefix"].isin(C.SN_COPY_PREFIXES) & a["num"].notna()]
    links = s.dropna(subset=["num"]).merge(copies, on="num", suffixes=("_sn", "_an"))
    links = pd.DataFrame({
        "sn_number": links["number"], "an_ticket_id": links["Ticket ID"], "correlation_id": links["correlation_id"],
        "sn_created": links["created_sn"], "an_created": links["created_an"],
        "lag_min": (links["created_an"] - links["created_sn"]).dt.total_seconds() / 60,
        "an_closed_by": links["Closed By"], "an_department": links["Department"],
    })
    return links


# ======================================================================= 4. enrich
def load_site_master(path=None):
    sm = pd.read_csv(path or C.SITE_MASTER, dtype=str).fillna("")
    sm["code"] = sm["code"].str.upper().str.strip()
    return sm


def _site_from_text(text, codes, prefixes):
    t = str(text).upper()
    m = re.search(r"\bMM[-_]([A-Z0-9]{4,6})[-_]", t)
    if m:
        return m.group(1)
    for code in codes:                         # exact codes anywhere as a token (e.g. -CHKPL-)
        if re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", t):
            return code
    for p in prefixes:                         # prefix codes at the start of a host name (CHK-, ZHB-, COLO...)
        if re.search(rf"(^|[\s:_-]){re.escape(p)}[-_\d]", t):
            return p
    if "-CHK-MT-" in t:
        return "CHKMT"
    return None


NOT_A_DEVICE = {"fortigate", "linux", "windows", "aruba", "cisco", "vmware", "microsoft", "group", "meraki", "juniper",
                "problem", "alert", "snow", "replication", "bandwidth", "ap_disconnected"}


def _looks_like_device(name):
    """A host name has a digit, '-' or '_' and is not a vendor/template word (e.g. 'FortiGate:' is not a device)."""
    n = name.strip(" :")
    return len(n) >= 4 and n.lower() not in NOT_A_DEVICE and bool(re.search(r"[\d_-]", n))


def _device(text):
    s = str(text)
    for pattern in (r"^AP\s+(\S+)\s+with (?:serial|MAC)", r"^(?:Meraki|Juniper) API_(\S+)", r"^(\S+?)\s+-\s+", r"^(\S+?)\s*:\s+",
                    r"for device:\s*(\S+)"):
        m = re.search(pattern, s)
        if m:
            name = re.sub(r"_\d+\.\d+\.\d+\.\d+.*$|-\d+\.\d+\.\d+\.\d+.*$", "", m.group(1))
            if _looks_like_device(name):
                return name
    m = re.search(r"Interface\s+([\w-]+)\(", s)
    if m:
        return "FortiGate " + m.group(1)
    return None


def _signature(text):
    s = re.sub(r"\d+(\.\d+){3}", "<ip>", str(text))
    s = re.sub(r"\b[\w-]*\d[\w-]*\b", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:90]


def _layer(text):
    t = str(text).lower()
    for name, rank, pat in C.LAYER_RULES:
        if re.search(pat, t):
            return name, rank
    return C.DEFAULT_LAYER, C.DEFAULT_RANK


def _strength(text):
    t = str(text).lower()
    return next((score for score, pat in C.STRENGTH_RULES if re.search(pat, t)), 1)


def _team(raw, layer, text):
    key = str(raw).strip().lower()
    if key and key not in ("nan", "<na>") and key not in C.BOT_TEAMS:
        return C.TEAM_ALIASES.get(key, str(raw).strip())
    return _team_for_layer(layer, text)


def _team_for_layer(layer, text):
    if layer == "server" and re.search(r"linux|lnx", str(text).lower()):
        return "Linux"
    return C.TEAM_FOR_LAYER.get(layer, "Triage")


def _city_from_location(text, sm):
    t = str(text).lower()
    for city in sorted(sm["city"].unique(), key=len, reverse=True):
        base = city.split(" (")[0].lower()
        if base and base not in ("unknown",) and base in t:
            return city
    aliases = {"nasik": "Nashik", "kandivli": "Kandivali", "zahirabad": "Zaheerabad"}
    return next((v for k, v in aliases.items() if k in t), None)


def build_events(a, s, links, sm):
    copy_ids = set(links["an_ticket_id"])
    an = a[~a["Ticket ID"].isin(copy_ids)].copy()
    an = an[~an["prefix"].fillna("").str.startswith(tuple(C.REQUEST_PREFIXES))]
    ev_an = pd.DataFrame({
        "event_id": an["Ticket ID"], "source": "AN", "created": an["created"], "closed": an["closed"],
        "title": an["Subject"], "raw_team": an["Department"], "closed_by": an["Closed By"],
        "kind": np.where(an["Ticket Type"].eq("Alert"), "alert", "infra ticket"),
        "location": None, "contact_type": None, "merged": False, "reopens": an["reopens"],
        "an_copy": None, "sheet": an["sheet"], "prefix": an["prefix"],
    })
    an_copy = links.drop_duplicates("sn_number").set_index("sn_number")["an_ticket_id"]
    ev_sn = pd.DataFrame({
        "event_id": s["number"], "source": "SN", "created": s["created"], "closed": s["closed"],
        "title": s["short_description"], "raw_team": s["assignment_group"], "closed_by": None,
        "kind": np.where(s["contact_type"].eq("Monitoring").fillna(False).to_numpy(bool), "monitoring ticket", "user ticket"),
        "location": s["caller_id.location"], "contact_type": s["contact_type"], "merged": s["merged"],
        "reopens": 0, "an_copy": s["number"].map(an_copy), "sheet": None, "prefix": None,
    })
    ev = pd.concat([ev_an, ev_sn], ignore_index=True)

    codes = sm.loc[sm["match"].eq("code"), "code"].tolist()
    prefixes = sm.loc[sm["match"].eq("prefix"), "code"].tolist()
    city_by_code = dict(zip(sm["code"], sm["city"]))
    ev["site"] = ev["title"].map(lambda t: _site_from_text(t, codes, prefixes))
    ev["city"] = ev["site"].map(city_by_code)
    users = ev["kind"].eq("user ticket") & ev["city"].isna()
    ev.loc[users, "city"] = (ev.loc[users, "location"].fillna("") + " " + ev.loc[users, "title"].fillna("")).map(
        lambda t: _city_from_location(t, sm))
    ev["device"] = ev["title"].map(_device)
    ev["device_known"] = ev["device"].notna() & ~ev["device"].astype("string").str.startswith("FortiGate ").fillna(False)
    lr = ev["title"].map(_layer)
    ev["layer"], ev["layer_rank"] = lr.str[0], lr.str[1]
    ev.loc[ev["prefix"].isin(C.SECURITY_PREFIXES), ["layer", "layer_rank"]] = ["security", 9]
    ev["strength"] = ev["title"].map(_strength)
    ev["signature"] = ev["title"].map(_signature)
    ev["team_today"] = [_team(r, l, t) for r, l, t in zip(ev["raw_team"], ev["layer"], ev["title"])]
    ev["layer_team"] = [_team_for_layer(l, t) for l, t in zip(ev["layer"], ev["title"])]
    ev["minutes_open"] = (ev["closed"] - ev["created"]).dt.total_seconds() / 60
    return ev.sort_values("created").reset_index(drop=True)


# ======================================================================= 5. noise
def reduce_noise(ev, st: Settings):
    ev = ev.copy()
    ev["self_healed"] = ev["closed_by"].isin(C.AUTO_CLOSERS) & (ev["minutes_open"] <= st.flap_min)
    key = np.where(ev["device_known"], "dev:" + ev["device"].astype(str) + "|" + ev["signature"],
                   np.where(st.text_fallback, "txt:" + ev["signature"], None))
    ev["repeat_of"] = None
    last = {}
    win = pd.Timedelta(minutes=st.dedupe_min)
    out = []
    for k, eid, t in zip(key, ev["event_id"], ev["created"]):
        if k is None:
            out.append(None); continue
        first_id, last_t = last.get(k, (None, None))
        if first_id is not None and t - last_t <= win:
            out.append(first_id); last[k] = (first_id, t)
        else:
            out.append(None); last[k] = (eid, t)
    ev["repeat_of"] = out
    return ev


# ======================================================================= 6. group
def _group_ids(keys, times, window_min):
    order = np.lexsort((times, keys))
    gid = np.empty(len(keys), dtype=np.int64)
    win = np.int64(window_min * 60 * 1_000_000_000)
    g, prev_key, prev_t = -1, None, None
    for i in order:
        if keys[i] != prev_key or times[i] - prev_t > win:
            g += 1
        gid[i] = g
        prev_key, prev_t = keys[i], times[i]
    return gid


def group_events(ev, st: Settings):
    ev = ev.copy()
    infra = ev["kind"].isin(["alert", "infra ticket", "monitoring ticket"]) & ev["layer"].ne("security")
    loc = ev["site"] if st.group_level == "site" else ev["city"]
    fallback = ("txt:" + ev["signature"].astype(str)) if st.text_fallback else ("solo:" + ev["event_id"].astype(str))
    key = np.where(loc.notna(), "loc:" + loc.astype(str),
                   np.where(ev["device_known"], "dev:" + ev["device"].astype(str), fallback))
    ev["group_key"] = np.where(infra, key, None)
    ev["incident_id"] = None
    sub = ev[infra]
    gid = _group_ids(sub["group_key"].to_numpy().astype(str),
                     sub["created"].to_numpy().astype("datetime64[ns]").astype("int64"), st.window_min)
    ev.loc[infra, "incident_id"] = [f"INC-C-{g + 1:05d}" for g in gid]
    return ev


# ======================================================================= 7. root & teams
def build_incidents(ev):
    m = ev.dropna(subset=["incident_id"]).copy()
    m = m.sort_values(["incident_id", "layer_rank", "strength", "created"], ascending=[True, True, False, True])
    roots = m.drop_duplicates("incident_id").set_index("incident_id")
    m["is_repeat"] = m["repeat_of"].notna()
    g = m.groupby("incident_id")
    # list-style columns: de-duplicate first, then join (fast even for 40k+ incidents)
    by_rank = m.drop_duplicates(["incident_id", "layer"])          # m is already sorted by layer_rank
    teams = m.dropna(subset=["team_today"]).drop_duplicates(["incident_id", "team_today"]).sort_values("team_today")
    srcs = m.drop_duplicates(["incident_id", "source"]).sort_values("source")
    inc = pd.DataFrame({
        "group_key": g["group_key"].first(),
        "site": g["site"].first(), "city": g["city"].first(),        # first non-empty value
        "start": g["created"].min(), "last_alert": g["created"].max(), "end": g["closed"].max(),
        "members": g.size(),
        "repeats": g["is_repeat"].sum().astype(int),
        "self_healed": g["self_healed"].sum().astype(int),
        "devices": g["device"].nunique(),
        "layers": by_rank.groupby("incident_id")["layer"].agg(", ".join),
        "teams_today": teams.groupby("incident_id")["team_today"].agg(list),
        "sources": srcs.groupby("incident_id")["source"].agg(", ".join),
    })
    inc["unique_alerts"] = inc["members"] - inc["repeats"]
    inc["teams_today"] = inc["teams_today"].apply(lambda v: v if isinstance(v, list) else [])
    inc["root_event_id"] = roots["event_id"]
    inc["root_title"] = roots["title"]
    inc["root_layer"] = roots["layer"]
    inc["root_device"] = roots["device"]
    inc["parent_team"] = roots["layer_team"]
    inc["root_is_earliest"] = roots["created"].eq(inc["start"])
    child = m[["incident_id", "layer_team"]].merge(inc[["parent_team"]], left_on="incident_id", right_index=True)
    child = child[child["layer_team"] != child["parent_team"]].drop_duplicates().sort_values("layer_team")
    inc["child_teams"] = child.groupby("incident_id")["layer_team"].agg(list)
    inc["child_teams"] = inc["child_teams"].apply(lambda v: v if isinstance(v, list) else [])
    power = inc["root_layer"].eq("power")
    inc.loc[power, "child_teams"] = inc.loc[power, "child_teams"].apply(lambda v: sorted(set(v) | {C.POWER_NOTIFY}))
    inc["n_teams_today"] = inc["teams_today"].apply(len)
    inc["teams_avoided"] = (inc["n_teams_today"] - 1).clip(lower=0)
    inc["tickets_avoided"] = inc["members"] - 1
    inc["confidence"] = np.select(
        [inc["group_key"].str.startswith("loc:") & inc["root_is_earliest"], inc["group_key"].str.startswith("loc:"),
         inc["group_key"].str.startswith("dev:")], ["high", "medium", "medium"], "low")   # txt: groups stay "low"
    inc.loc[inc["members"] == 1, "confidence"] = "single"
    place = inc["site"].fillna(inc["city"]).fillna("unknown").astype(object)
    dev = inc["root_device"].fillna("unknown").astype(object)
    inc["why"] = [
        "single alert - nothing to group" if n == 1 else
        (f"same plant ({p})" if k.startswith("loc:") else f"same device ({d})" if k.startswith("dev:")
         else "identical alert text, device unknown (low confidence)")
        + f" · alerts within the time window · primary = highest layer '{lay}'" + (" and earliest alert" if e else "")
        for n, k, p, d, lay, e in zip(inc["members"], inc["group_key"], place, dev, inc["root_layer"], inc["root_is_earliest"])]
    return inc.reset_index().sort_values("start").reset_index(drop=True)


# ======================================================================= 8. attach user tickets
def attach_users(ev, inc, st: Settings):
    users = ev[ev["kind"].eq("user ticket") & ev["city"].notna() & ev["layer"].ne("security")]
    cand = inc[(inc["members"] >= 2) & inc["city"].notna()]
    rows = []
    if len(users) and len(cand):
        j = users[["event_id", "created", "city", "title"]].merge(
            cand[["incident_id", "city", "start", "last_alert", "layers"]], on="city")
        j = j[(j["created"] >= j["start"] - pd.Timedelta(minutes=st.attach_before_min)) &
              (j["created"] <= j["last_alert"] + pd.Timedelta(minutes=st.attach_after_min))]
        for r in j.itertuples():
            text = str(r.title).lower()
            fits = [layers for pat, layers in C.USER_SYMPTOM_LAYERS.items() if re.search(pat, text)]
            fit_layer = any(l in r.layers for layers in fits for l in layers)
            outage = bool(re.search(C.GENERIC_OUTAGE_WORDS, text))
            if fit_layer and outage:            # must describe an outage AND fit what is failing
                gap = abs((r.created - r.start).total_seconds()) / 60
                rows.append({"user_ticket": r.event_id, "incident_id": r.incident_id, "minutes_from_start": round(gap, 1),
                             "reason": f"outage reported · symptom fits incident layers · same city ({r.city})"})
    links = pd.DataFrame(rows, columns=["user_ticket", "incident_id", "minutes_from_start", "reason"])
    if len(links):
        links = links.sort_values("minutes_from_start").drop_duplicates("user_ticket")
    return links


# ======================================================================= 9. score
def score(ev, inc, users, links, n_requests, st: Settings):
    infra = ev.dropna(subset=["incident_id"])
    multi = inc[inc["members"] >= 2]
    storms = inc[(inc["members"] >= 5) & (inc["devices"] >= 2)]
    sn_merged = ev[(ev["source"] == "SN") & ev["merged"]]
    merged_in_group = sn_merged["incident_id"].isin(multi["incident_id"]) | sn_merged["event_id"].isin(users["user_ticket"])
    k = {
        "events_in": int(len(infra)),
        "incidents_out": int(len(inc)),
        "compression": round(len(infra) / max(len(inc), 1), 2),
        "ticket_reduction_pct": round(100 * (1 - len(inc) / max(len(infra), 1)), 1),
        "multi_alert_incidents": int(len(multi)),
        "alerts_in_multi": int(multi["members"].sum()),
        "storms": int(len(storms)),
        "storm_compression": round(storms["members"].sum() / max(len(storms), 1), 1),
        "teams_avoided": int(inc["teams_avoided"].sum()),
        "self_healed": int(infra["self_healed"].sum()),
        "repeats": int(infra["repeat_of"].notna().sum()),
        "sn_an_links": int(len(links)),
        "user_tickets": int((ev["kind"] == "user ticket").sum()),
        "user_tickets_attached": int(len(users)),
        "sn_merged": int(len(sn_merged)),
        "sn_merged_grouped_pct": round(100 * merged_in_group.mean(), 1) if len(sn_merged) else 0.0,
        "site_coverage_pct": round(100 * infra["site"].notna().mean(), 1),
        "device_coverage_pct": round(100 * (infra["site"].notna() | infra["device_known"]).mean(), 1),
        "requests_excluded": int(n_requests),
        "settings": asdict(st),
    }
    return k


# ======================================================================= run everything
def prepare(raw, site_master=None):
    """Steps 1-4: slow part, independent of the knobs - cache this."""
    a = normalise_alfa(raw["alfa"])
    s = normalise_sn(raw["sn"])
    links = link_sn_an(a, s)
    sm = site_master if site_master is not None else load_site_master()
    ev = build_events(a, s, links, sm)
    n_req = int(a["prefix"].fillna("").str.startswith(tuple(C.REQUEST_PREFIXES)).sum() + len(raw["sn_requests"]))
    return {"events": ev, "links": links, "n_requests": n_req, "alfa_rows": len(raw["alfa"]), "alfa_tickets": len(a)}


def correlate(prepared, st: Settings | None = None):
    """Steps 5-9: fast part, re-run whenever a knob changes."""
    st = st or Settings()
    ev = reduce_noise(prepared["events"], st)
    ev = group_events(ev, st)
    inc = build_incidents(ev)
    users = attach_users(ev, inc, st)
    inc["user_tickets"] = inc["incident_id"].map(users.groupby("incident_id").size()).fillna(0).astype(int)
    ev["attached_to"] = ev["event_id"].map(users.set_index("user_ticket")["incident_id"]) if len(users) else None
    k = score(ev, inc, users, prepared["links"], prepared["n_requests"], st)
    return {"events": ev, "incidents": inc, "user_links": users, "sn_an_links": prepared["links"], "kpis": k}


def run(st: Settings | None = None, files=None):
    return correlate(prepare(load_raw(files)), st)
