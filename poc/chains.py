"""
Dependency-chain test for P1/P2 alerts.

Question: when a network device at a site goes down, do we see one ticket per downstream layer (VM, server,
database, application) at the same site, and how long after? Which time-window gap finds those chains?

A *chain* = a group of alerts at one location where each alert is within `window` seconds of the previous one,
and at least two different layers are present. Its *spine* = the first alert of each layer, in time order.
Repeats and FortiGate twins are left out, so a chain has one ticket per device per layer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .engine import _group_ids

DC_LOCATIONS = {"MDE", "MDE-COLO", "MRV-COLO", "MRV-CHENNAI"}           # data centres: where VMs / DBs / apps live
WINDOWS_S = [30, 45, 60, 90, 120, 180, 300, 600, 900]
LAYER_RANK = {name: rank for name, rank, _ in C.LAYER_RULES}


def spine_alerts(ev, impact=None, links=None, inc=None):
    """Alerts that may form a chain: real alerts (no recoveries), not repeats or twins, with a location.
    With `impact` + `links` + `inc`: the user tickets / database alerts linked to an incident join as that incident's
    location, so a chain can continue into the database / application layer."""
    cols = ["event_id", "created", "location", "layer", "layer_rank", "device", "priority", "team", "title", "repeat_of", "kind"]
    a = ev.loc[ev["kind"].eq("alert") & ev["repeat_of"].isna() & ev["location"].notna(), cols]
    if impact is not None and links is not None and len(links):
        li = links.drop(columns=["kind"]).merge(impact, on="impact_id").merge(inc[["incident_id", "location"]], on="incident_id")
        extra = pd.DataFrame({
            "event_id": li["impact_id"], "created": li["created"], "location": li["location"], "layer": li["layer"],
            "layer_rank": li["layer"].map(LAYER_RANK), "device": li["kind"], "priority": li["priority"].astype(str).str.slice(0, 2),
            "team": li["team"], "title": li["title"], "repeat_of": None, "kind": "alert",
        })
        a = pd.concat([a, extra], ignore_index=True)
    return a.sort_values("created").reset_index(drop=True)


def chains_for_window(a, window_s, key=None):
    """Group alerts with a rolling window of `window_s` seconds per location (or per `key`), one row per group."""
    keys = (a["location"] if key is None else key).to_numpy().astype(str)
    gid = _group_ids(keys, a["created"].to_numpy().astype("datetime64[ns]").astype("int64"), window_s / 60)
    a = a.assign(chain=gid)
    g = a.groupby("chain")
    spine = a.drop_duplicates(["chain", "layer"])                      # a is sorted by time: first alert per layer
    s = spine.groupby("chain")
    most_up = spine.sort_values(["chain", "layer_rank"], kind="stable").drop_duplicates("chain").set_index("chain")["layer"]
    out = pd.DataFrame({
        "location": g["location"].first(), "locations": g["location"].nunique(),
        "start": g["created"].min(), "end": g["created"].max(),
        "alerts": g.size(), "devices": g["device"].nunique(),
        "n_layers": s.size(),
        "spine": s["layer"].agg(" → ".join),
        "span_s": (s["created"].max() - s["created"].min()).dt.total_seconds(),
        "first_detected": s["layer"].first(),
        "most_upstream": most_up,
    })
    out["upstream_detected_first"] = out["first_detected"].eq(out["most_upstream"])
    out = out[out["n_layers"] >= 2].sort_values(["n_layers", "alerts"], ascending=False)
    return out.reset_index(), a[["event_id", "chain"]]


def window_sweep(a, windows=WINDOWS_S):
    rows = []
    for w in windows:
        ch, _ = chains_for_window(a, w)
        rows.append({"window_s": w, "chains (2+ layers)": len(ch), "chains (3+ layers)": int((ch["n_layers"] >= 3).sum()),
                     "alerts in chains": int(ch["alerts"].sum()), "upstream detected first %": round(100 * ch["upstream_detected_first"].mean(), 1) if len(ch) else 0.0,
                     "median span s": float(ch["span_s"].median()) if len(ch) else 0.0})
    return pd.DataFrame(rows)


def spine_gaps(a, window_s=900):
    """Within chains found at a wide window: the gap between consecutive spine layers (how long until the next layer shows up)."""
    ch, members = chains_for_window(a, window_s)
    m = a.merge(members, on="event_id").merge(ch[["chain"]], on="chain")
    sp = m.drop_duplicates(["chain", "layer"]).sort_values(["chain", "created"])
    sp["gap_s"] = sp.groupby("chain")["created"].diff().dt.total_seconds()
    sp["from_layer"] = sp.groupby("chain")["layer"].shift()
    sp = sp.dropna(subset=["gap_s"])
    return sp[["chain", "location", "from_layer", "layer", "gap_s", "created"]].rename(columns={"layer": "to_layer"})


def cross_location_test(a, window_s):
    """Does a plant chain have a data-centre alert of a downstream layer within the window? Compared with a
    baseline where the data-centre alerts are shifted by 6 hours (what coincidence alone would give)."""
    plant = a[~a["location"].isin(DC_LOCATIONS)]
    dc = a[a["location"].isin(DC_LOCATIONS)]
    ch, _ = chains_for_window(plant, window_s) if len(plant) else (pd.DataFrame(), None)
    if ch.empty or dc.empty:
        return {"plant_chains": int(len(ch)), "with_dc_alert_in_window": 0, "baseline_shifted_6h": 0}

    def count(dc_times):
        dc_times = np.sort(dc_times.astype("datetime64[ns]").astype("int64"))
        w = np.int64(window_s * 1_000_000_000)
        starts = ch["start"].to_numpy().astype("datetime64[ns]").astype("int64")
        ends = ch["end"].to_numpy().astype("datetime64[ns]").astype("int64")
        lo = np.searchsorted(dc_times, starts - w)
        hi = np.searchsorted(dc_times, ends + w)
        return int((hi > lo).sum())
    actual = count(dc["created"].to_numpy())
    shifted = count((dc["created"] + pd.Timedelta(hours=6)).to_numpy())
    return {"plant_chains": int(len(ch)), "with_dc_alert_in_window": actual, "baseline_shifted_6h": shifted}


def chain_members(a, members, chain_id):
    m = a.merge(members[members["chain"] == chain_id], on="event_id").sort_values("created")
    m["gap_s"] = m["created"].diff().dt.total_seconds().fillna(0)
    m["spine"] = ~m["layer"].duplicated()
    return m
