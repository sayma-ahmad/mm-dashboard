
"""
Settings and rules for the correlation POC.

Everything a non-programmer may want to change lives here: file paths, time zones,
layer rules, team mapping and keyword lists. The engine (engine.py) only reads these.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where the client exports live.
# Priority:
#   1. MM_DATA_DIR environment variable (server/cloud deployment)
#   2. data/raw inside the project (local or cloud)
#   3. Usual laptop folders (local development)
_INPUT_CANDIDATES = (
    [Path(os.environ["MM_DATA_DIR"])]
    if os.environ.get("MM_DATA_DIR")
    else []
) + [
    PROJECT_ROOT / "data" / "raw",
    Path.home() / "OneDrive" / "Desktop" / "M&M",
    Path.home() / "Downloads",
]

INPUT_DIR = next(
    (
        directory
        for directory in _INPUT_CANDIDATES
        if any(directory.glob("Alfa_*Tickets*.xlsx"))
    ),
    _INPUT_CANDIDATES[0],
)

FILES = {
    "alfa": INPUT_DIR / "Alfa_Open_Resolved_Closed Tickets_Oct 26.xlsx",
    "sn_incidents": INPUT_DIR / "Msolve incident_Oct 26.csv",
    "sn_requests": INPUT_DIR / "Msolve service req_Oct 26.csv",
    "rvtool": INPUT_DIR / "Copy of RVTool Club Data - Sep 2026.xlsx",
}

SITE_MASTER = Path(__file__).resolve().parent / "site_master.csv"
NOMENCLATURE_DIR = (
    Path(__file__).resolve().parent / "nomenclature"
)  # built by build_nomenclature.py + site_codes.csv

OUTPUT_DIR = PROJECT_ROOT / "outputs" / "poc"

# ---------------------------------------------------------------- P1/P2 scope (p1p2_engine.py)
P1P2_PREFIXES = ["IMZA", "ALZA"]  # Zabbix tickets: only in Alfa, never in Msolve
P1P2_PRIORITIES = ["P1 Critical", "P2 High"]
RECOVERY_WORDS = (
    r"link status is up|\bis up\b|has been restored|is reachable again|"
    r"recovered|back online"
)
CORE_DEVICE_WORDS = r"mdf|core|csw"
FORTIGATE_TWIN_SECONDS = 20

# ---------------------------------------------------------------- time zones
# Confirmed from the data: the Alfa "Closed" sheet is UTC (Created On AND Closed On),
# the Open/Resolved sheets and Msolve are IST. Everything is converted to IST.
ALFA_UTC_SHEETS = ["Closed"]
UTC_TO_IST_HOURS = 5.5

# ---------------------------------------------------------------- ticket kinds
SN_COPY_PREFIXES = ["IMSN", "IMEP", "SRSN", "SREP", "IMAT", "SRAT"]
REQUEST_PREFIXES = ["SR"]
SECURITY_PREFIXES = ["IMEP"]
AUTO_CLOSERS = ["System"]

# ---------------------------------------------------------------- layers
# Order matters: the first rule whose pattern matches the ticket text wins.
# rank = position in the dependency chain (lower = more upstream = more likely the primary problem)
LAYER_RULES = [
    ("security", 9, r"phishing|case id|custom_|cyberdefence|malware|siem"),
    ("power", 0, r"power-reset|power loss|cold hw reset|\bups\b|power fail"),
    ("network edge", 1, r"fortigate|sdw|-edg-|\bwan\b|\bill\b|ovmum_ill|mpls|bgp|\bisp\b|leased line"),
    (
        "network",
        2,
        r"switch|nexus|n9k|cisco ios|aruba|juniper|huawei|core_sw|snmp|icmp|meraki|"
        r"ap_disconnected|aps went down|\bap\b|-ap\d|access point|bandwidth|interface|"
        r"router|network generic|wifi|wi-fi|infoblox|dns|dhcp",
    ),
    ("virtualization", 3, r"vmware|esx|hypervisor|vcenter|datastore|hyper-v|nutanix|\bahv\b"),
    ("storage", 3, r"\bsan\b|storage|netapp|\blun\b|backup|tape|veeam"),
    ("database", 5, r"oracle|sql|database|\bdb\b|tablespace|listener"),
    (
        "server",
        4,
        r"_win_|windows|linux|zabbix agent|\bcpu\b|memory|disk|host has been restarted|"
        r"replication|server",
    ),
    (
        "application",
        6,
        r"\bsap\b|\bmes\b|http|application|portal|exchange|sharepoint|share point|"
        r"o365|microsoft|intune|outlook|teams",
    ),
]

DEFAULT_LAYER, DEFAULT_RANK = "other", 7

# Symptom strength: higher = stronger evidence of a real outage (used to break ties for the primary problem)
STRENGTH_RULES = [
    (5, r"power|ups"),
    (4, r"link down|link status is down|is down|went down|interface .* down|bgp"),
    (3, r"unreachable|unavailable|icmp|not available|disconnected|no snmp|not responding"),
    (2, r"restarted|reboot|uptime"),
    (1, r"cpu|memory|disk|temperature|fan|bandwidth|latency|response time"),
]

# ---------------------------------------------------------------- teams
TEAM_FOR_LAYER = {
    "power": "Network",
    "network edge": "Network",
    "network": "Network",
    "virtualization": "Virtualization",
    "storage": "Storage",
    "server": "Windows",
    "database": "Database",
    "application": "Applications",
    "security": "Security",
    "other": "Triage",
}

# Real team names in Alfa (Department) and Msolve (assignment_group) -> one list
TEAM_ALIASES = {
    "rid-network": "Network",
    "rid_network": "Network",
    "rid network": "Network",
    "rid_windows": "Windows",
    "rid-windows": "Windows",
    "rid_linux": "Linux",
    "rid-linux": "Linux",
    "rid_storage": "Storage",
    "rid-storage": "Storage",
    "rid_virtualization": "Virtualization",
    "rid-virtualization": "Virtualization",
    "rid_zabbix_operations": "Zabbix Ops",
    "internal exchange": "Messaging",
    "share point": "Collaboration",
    "intune": "Endpoint",
    "doc support": "Doc Support",
}

BOT_TEAMS = ["rid-bot"]
POWER_NOTIFY = "Facilities"

# ---------------------------------------------------------------- user tickets (link 3)
# Words in a user ticket -> the layers it could be a symptom of
USER_SYMPTOM_LAYERS = {
    r"network|internet|wifi|wi-fi|lan|connectivity|vpn|zscaler|link|switch": [
        "power",
        "network edge",
        "network",
    ],
    r"\bsap\b|\bmes\b|application|portal|url|website|login|outlook|email|mail|teams": [
        "application",
        "database",
        "server",
        "network edge",
        "network",
    ],
    r"server|rdp|remote|share|folder|drive|path": [
        "server",
        "virtualization",
        "storage",
        "network edge",
        "network",
    ],
}

GENERIC_OUTAGE_WORDS = (
    r"not working|down|unable|cannot|can't|not able|slow|"
    r"not accessible|not opening|error"
)
