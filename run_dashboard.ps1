# Starts the P1/P2 alert-correlation dashboard on http://<this machine>:8502
# First run creates .venv and installs the packages (needs internet once). Ctrl+C stops it.
# Data: put the client exports in data\raw, or set MM_DATA_DIR to the folder that holds them. See DEPLOY.md.
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating the virtual environment (.venv)..."
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { py -3.13 -m venv .venv } else { python -m venv .venv }
    if (-not (Test-Path ".venv\Scripts\python.exe")) { Write-Error "Python 3.13 is needed. Install it from python.org and run this script again."; exit 1 }
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.\.venv\Scripts\python.exe -m pip install -r requirements-dashboard.txt --quiet

if ($env:MM_DATA_DIR) { Write-Host "Data folder: $env:MM_DATA_DIR" } else { Write-Host "Data folder: data\raw (set MM_DATA_DIR to use another folder)" }
Write-Host "Starting the dashboard on port 8502. The first page load takes about 90 seconds while the engine runs. Ctrl+C stops it."
.\.venv\Scripts\python.exe -m streamlit run poc\p1p2_app.py
