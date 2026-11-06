# Mahindra & Mahindra – Alert Storm Analysis

This project proves (with numbers) that one infrastructure failure creates many alerts and many tickets
across different teams, and tests simple correlation rules that would group them.

```
mahindra-analysis/
├── .venv/                       Python environment for this project (don't edit)
├── data/
│   ├── raw/                     put the client's real exports here (never commit / share)
│   └── sample/                  fake practice data (made by generate_sample_data.py)
├── notebooks/
│   └── incident_analysis.ipynb  the analysis – 8 sections + summary
├── outputs/
│   ├── charts/                  PNG charts (created when you run the notebook)
│   └── reports/                 backtest_samples.xlsx + findings_summary.txt
├── generate_sample_data.py      makes the fake data
├── requirements.txt             exact package versions
├── data_request_checklist.md    what to ask the client for
└── README.md                    this file
```

## 1. Open a terminal in the project folder

Open **PowerShell** and run:

```powershell
cd "$HOME\OneDrive\Documents\mahindra-analysis"
```

## 2. Activate the virtual environment

The virtual environment (`.venv`) is a private copy of Python with this project's packages.
"Activating" it means the commands `python` and `jupyter` use that copy.

```powershell
.\.venv\Scripts\Activate.ps1
```

You'll see `(.venv)` at the start of the prompt. To leave it: `deactivate`.

> If PowerShell says *"running scripts is disabled on this system"*, run this once, then try again:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## 3. (Re)create the practice data

```powershell
python generate_sample_data.py
```

## 4. Open Jupyter

**Option A – JupyterLab in the browser**

```powershell
jupyter lab
```

Open `notebooks/incident_analysis.ipynb`, check the kernel in the top-right says
**Python (mahindra-analysis)**, then menu **Run → Run All Cells**.

**Option B – VS Code**

Open the project folder in VS Code (`code .`), open the notebook, click **Select Kernel** (top-right)
and choose **Python (mahindra-analysis)** or the `.venv` interpreter. Then click **Run All**.
VS Code needs the **Python** and **Jupyter** extensions from Microsoft.

## 5. Switch from sample to real data

1. Copy the client's exports into `data/raw/` (CSV or Excel both work).
2. In the notebook's first code cell (**Settings**):
   * set `DATA_SOURCE = "raw"`
   * set the real file names in `FILES["raw"]`
   * if the file is Excel with several sheets, set `EXCEL_SHEET` to the sheet name
3. Map the columns (next section), check dates/time zones, then **Run All**.

## 6. Map the client's column names

The notebook uses standard names internally. You only tell it which client column means what.
Left side = standard name (don't change). Right side = the client's column header (change this).

```python
ALERT_COLUMNS = {
    "alert_id":   "eventid",        # client calls it "eventid"
    "start_time": "Time",
    "end_time":   "Recovery time",
    "location":   "Host group",
    "host":       "Host",
    "alert_type": None,             # client has no type column -> guessed from the message text
    "severity":   "Severity",
    "message":    "Problem",
    ...
}
```

* Use `None` when the client doesn't have that column. Only these are compulsory:
  alerts – `start_time`, `location`, `message`; tickets – `created_at`, `location`, `assignment_group`.
* If a name is wrong, the notebook stops with a message listing the columns that *are* in the file,
  so you can copy the exact spelling.
* If `alert_type` is `None`, types are guessed from keywords in `TYPE_KEYWORDS` (in the loading cell).
  If the "other" bar is large, add the client's words to that list.

**Dates:** if dates look like `25/10/2026`, set `DAYFIRST = True`.
**Time zones:** Zabbix exports are often in UTC and ServiceNow in IST. If so, set `ALERTS_TIMEZONE = "UTC"`
so alerts and tickets line up. Otherwise storms won't match their tickets.

## 7. Tune the analysis

All knobs are in the Settings cell, for example `STORM_WINDOW_MIN = 15` and `STORM_MIN_ALERTS = 5`.
Change one, re-run all, and see how the numbers move. That's how you choose the rule to propose.

## 8. Real client data (Msolve + Alfa)

The client's first real exports (Msolve incidents, Msolve service requests, Alfa tickets) don't fit the
practice notebook's format, so they have their own tools:

* `link_msolve_alfa.py` links Msolve tickets to Alfa tickets (Msolve `correlation_id` = the number in the Alfa
  `Ticket ID`). Output: `outputs/reports/msolve_alfa_linked.xlsx`.
* `notebooks/real_data_analysis.ipynb` contains the full analysis of the real data. Charts go to `outputs/charts/real/`,
  the summary to `outputs/reports/real_findings_summary.txt`, and the backtest examples to `outputs/reports/real_backtest_samples.xlsx`.

Both read the files from your **Downloads** folder. Update the file names at the top of each when new exports arrive.

## 9. Correlation POC demo

The `poc/` folder is a working demo of the correlation engine, running on the real October exports.

| File | What it is |
|---|---|
| `poc/config.py` | All rules and settings: file paths, time zones, layer rules, team mapping, keywords |
| `poc/site_master.csv` | Plant code → city. A **starter list with guessed cities**: confirm it with the network team |
| `poc/engine.py` | The engine: load → link SN↔AN → enrich → reduce noise → group → primary problem & teams → attach user tickets → KPIs |
| `poc/run_backtest.py` | Runs the engine once and writes `outputs/poc/poc_backtest.xlsx` (KPIs, incidents, members, links, a 30-incident review sheet) |
| `poc/app.py` | The demo web app |

**Run the demo app** (venv active, in the project folder):

```powershell
streamlit run poc/app.py
```

A browser tab opens at http://localhost:8501. The first load takes about 20 seconds. Move the sliders in the sidebar to change the rules and watch the numbers update. Press `Ctrl+C` in PowerShell to stop it.

**Run the backtest only:**

```powershell
python -m poc.run_backtest
```

**Change the rules:** edit `poc/config.py` (e.g. add a keyword to a layer) or `poc/site_master.csv` (e.g. correct a city), then refresh the app.

## 10. P1/P2 correlation POC (IMZA + ALZA, RVTool nomenclature)

A second, narrower engine for what ops act on: **P1/P2 Zabbix tickets** (`IMZA`, `ALZA`). These exist only in Alfa, never in
Msolve, so there is no `correlation_id`; each alert is correlated from four things: **location** (RVTool nomenclature),
**timestamp**, **text** (layer + symptom) and **dependency** (VM → ESXi host → cluster, from RVTool).

| File | What it is |
|---|---|
| `poc/build_nomenclature.py` | Reads the RVTool export and writes `poc/nomenclature/rv_*.csv`: server names, ESXi hosts and their VMs, naming prefixes, /24 subnets, locations |
| `poc/nomenclature/site_codes.csv` | Hand-written map of alert site codes (`MM-NSKP1-...`) → RVTool location. Confirm the rows marked *guess* / *medium* |
| `poc/p1p2_engine.py` | The engine: load → place (location ladder) → enrich → noise → group → primary problem by dependency/layer → KPIs |
| `poc/run_p1p2_backtest.py` | Writes `outputs/poc/p1p2_backtest.xlsx` (KPIs, incidents, members, locations, site codes, unplaced, 30-incident review sheet) |
| `poc/p1p2_app.py` | The demo web app |

**Run it** (venv active, in the project folder):

```powershell
python -m poc.build_nomenclature
```

```powershell
streamlit run poc/p1p2_app.py --server.port 8502
```

```powershell
python -m poc.run_p1p2_backtest
```

The RVTool file name is in `FILES["rvtool"]` in `poc/config.py`. Re-run `build_nomenclature` whenever a new inventory arrives.

## 11. Deploying the dashboard for the team

See [DEPLOY.md](DEPLOY.md). Short version: unzip the package, put the four client export files in `data/raw/`
(or set `MM_DATA_DIR`), run `run_dashboard.ps1` (Windows) or `run_dashboard.sh` (Linux), or `docker compose up -d --build`,
then open `http://<host>:8502`. `requirements-dashboard.txt` holds the minimal packages; the full notebook
environment stays in `requirements.txt`.

## Notes

* **OneDrive:** this folder is inside OneDrive, so `.venv` (thousands of small files) will sync too.
  That's harmless but slow. To skip it, right-click `.venv` → **Free up space**, or move the project
  out of OneDrive later. Also check the client is OK with their data in your OneDrive before you put it in `data/raw/`.
* **Rebuilding the environment** on another machine:
  `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
* `data/sample/storms_ground_truth.csv` lists the storms planted in the fake data. The notebook uses it to
  check that storm detection works (it should find 60 of 60).
