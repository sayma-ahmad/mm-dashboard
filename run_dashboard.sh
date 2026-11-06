#!/usr/bin/env bash
# Starts the P1/P2 alert-correlation dashboard on http://<this machine>:8502
# First run creates .venv and installs the packages (needs internet once). Ctrl+C stops it.
# Data: put the client exports in data/raw, or set MM_DATA_DIR to the folder that holds them. See DEPLOY.md.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtual environment (.venv)..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements-dashboard.txt --quiet

echo "Data folder: ${MM_DATA_DIR:-data/raw}"
echo "Starting the dashboard on port 8502. The first page load takes about 90 seconds while the engine runs. Ctrl+C stops it."
exec .venv/bin/python -m streamlit run poc/p1p2_app.py
