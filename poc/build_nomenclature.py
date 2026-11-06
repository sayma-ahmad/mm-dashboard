"""
Build the location nomenclature from the client's RVTool inventory.

    python -m poc.build_nomenclature

Reads FILES["rvtool"] (poc/config.py) and writes poc/nomenclature/:
  rv_locations.csv  location -> city, number of VMs, clusters
  rv_hosts.csv      VM / server name -> location, datacenter, cluster, ESXi host it runs on, application
  rv_esxi.csv       ESXi / AHV host -> location, cluster, the VMs on it (the dependency chain)
  rv_prefixes.csv   first 5-6 letters of a host name -> location (only prefixes that are >= 85% one location)
  rv_subnets.csv    /24 subnet -> location (only subnets that are >= 90% one location)

site_codes.csv in the same folder is hand-written: alert site codes (MM-NSKP1-...) -> RVTool location.
"""
import re
from pathlib import Path

import pandas as pd

from . import config as C

# Location name -> city. Anything not listed falls back to the last word of the location name.
CITY_OF = {
    "MT-CHAKAN": "Chakan", "MVML-CHAKAN": "Chakan", "MHEL CHAKAN": "Chakan", "MDSL-CHAKAN": "Chakan",
    "MAHINDRA ACCELO CHAKAN PLANT1": "Chakan", "MRV-CHENNAI": "Chennai", "MRV-COLO": "Chennai", "MSPT-CHENNAI": "Chennai",
    "MAHINDRA ACCELO CHENNAI": "Chennai", "MDE": "Kandivali", "MDE-COLO": "Kandivali", "GATEWAY HO": "Mumbai", "WORLI": "Mumbai",
    "MLDL-WORLI": "Mumbai", "SEWRI": "Mumbai", "MLDL-CHANDIVALI": "Mumbai", "NASIK PLANT 1": "Nashik", "NASIK PLANT 2": "Nashik",
    "MAHINDRA ACCELO NASHIK": "Nashik", "NAGPUR 1": "Nagpur", "NAGPUR 2": "Nagpur", "KANHE 1": "Kanhe", "KANHE 2": "Kanhe",
    "JAIPUR FD": "Jaipur", "JAIPUR-SBU": "Jaipur", "ZAHEERABAD-AD": "Zaheerabad", "ZAHEERABAD-FD": "Zaheerabad",
    "LMM-BANGALORE": "Bangalore", "VTP-BANGALORE": "Bangalore", "IC-BANGALORE": "Bangalore", "MEML-BANGALORE": "Bangalore",
    "SDVC-COIMBATORE": "Coimbatore", "MASPL-KOLAR": "Kolar", "MLDL-PUNE -CITADEL": "Pune", "MLDL-PUNE-PIMPRI": "Pune",
    "MLDL-PUNE-TATHAWADE": "Pune", "ACCELO SUPA AHMEDMAGAR": "Ahmednagar", "CLOUD": "Cloud",
}
NOT_A_HOST = {"CISCO", "VMWARE", "NUTANIX", "WINDOWS", "LINUX", "FORTIGATE", "ARUBA", "HUAWEI", "TERMINAL", "PROFILE",
              "CONSOLE", "SERVER", "BACKUP", "TEST", "TEMP", "CLONE", "VCENTER"}


def host_token(name):
    """'MMJPRDRNAPP1_migrated, but...' -> 'MMJPRDRNAPP1'; 'CVM-FCC01 (1)' -> 'CVM-FCC01'."""
    t = re.split(r"[ (_,]", str(name).upper().strip())[0].strip("-.")
    return t


def esxi_key(host):
    """'10.2.148.145' -> same; 'mimnuac07/AHV' -> 'MIMNUAC07'."""
    return str(host).split("/")[0].strip().upper()


def build(rv_path=None, out_dir=None):
    rv = pd.read_excel(rv_path or C.FILES["rvtool"], dtype=str)
    out = Path(out_dir or C.NOMENCLATURE_DIR)
    out.mkdir(parents=True, exist_ok=True)
    rv["location"] = rv["Location"].str.upper().str.strip()
    rv = rv.dropna(subset=["location"]).copy()
    rv["city"] = rv["location"].map(lambda l: CITY_OF.get(l, l.replace("-", " ").split()[-1].title()))
    rv["host"] = rv["Host Name"].map(host_token)
    rv["esxi"] = rv["Host"].map(esxi_key).replace({"NAN": ""})
    rv["ip"] = rv["Primary IP Address"].fillna("").str.strip()

    locs = rv.groupby("location").agg(city=("city", "first"), vms=("host", "size"),
                                      clusters=("Cluster", lambda s: ", ".join(sorted(s.dropna().unique())))).reset_index()
    locs.to_csv(out / "rv_locations.csv", index=False)

    hosts = rv[(rv["host"].str.len() >= 5) & ~rv["host"].isin(NOT_A_HOST)]
    hosts = hosts.drop_duplicates("host")[["host", "Host Name", "ip", "location", "city", "Datacenter", "Cluster", "esxi",
                                           "Application_Name", "Operating System", "Category", "Powerstate"]]
    hosts.columns = ["host", "host_name", "ip", "location", "city", "datacenter", "cluster", "esxi_host", "application", "os",
                     "category", "powerstate"]
    hosts.to_csv(out / "rv_hosts.csv", index=False)

    e = rv[rv["esxi"].ne("")].groupby("esxi").agg(
        location=("location", lambda s: s.value_counts().index[0]), city=("city", "first"),
        cluster=("Cluster", lambda s: s.dropna().value_counts().index[0] if s.notna().any() else ""),
        datacenter=("Datacenter", "first"), n_vms=("host", "size"),
        vms=("host", lambda s: " | ".join(s)),
        apps=("Application_Name", lambda s: " | ".join(sorted(s.dropna().unique())))).reset_index()
    e.to_csv(out / "rv_esxi.csv", index=False)

    rows = []
    for n in (6, 5):
        p = rv.assign(prefix=rv["host"].str.slice(0, n))
        p = p[~p["prefix"].str.match(r"^\d")]                       # skip names that are IP addresses
        g = p.groupby("prefix")["location"].agg(n="size", location=lambda s: s.value_counts().index[0],
                                                share=lambda s: s.value_counts().iloc[0] / s.size).reset_index()
        g = g[(g["n"] >= 4) & (g["share"] >= 0.85) & (g["prefix"].str.len() == n)]
        rows.append(g.assign(length=n))
    pfx = pd.concat(rows).sort_values(["length", "n"], ascending=[False, False])
    pfx["city"] = pfx["location"].map(dict(zip(locs["location"], locs["city"])))
    pfx[["prefix", "length", "location", "city", "n", "share"]].to_csv(out / "rv_prefixes.csv", index=False)

    s = rv.assign(net24=rv["ip"].str.extract(r"^(\d+\.\d+\.\d+)\.")[0]).dropna(subset=["net24"])
    s = s[~s["net24"].str.startswith("169.254")]                     # link-local, meaningless
    g = s.groupby("net24")["location"].agg(n="size", location=lambda x: x.value_counts().index[0],
                                           share=lambda x: x.value_counts().iloc[0] / x.size).reset_index()
    g = g[(g["n"] >= 2) & (g["share"] >= 0.9)]
    g["city"] = g["location"].map(dict(zip(locs["location"], locs["city"])))
    g[["net24", "location", "city", "n", "share"]].to_csv(out / "rv_subnets.csv", index=False)

    return {"locations": len(locs), "hosts": len(hosts), "esxi_hosts": len(e), "prefixes": len(pfx), "subnets": len(g)}


if __name__ == "__main__":
    print(build())
