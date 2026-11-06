# P1/P2 alert-correlation dashboard. Build: docker build -t mm-dashboard .   Run: see docker-compose.yml or DEPLOY.md
FROM python:3.13-slim

WORKDIR /app
COPY requirements-dashboard.txt .
RUN pip install --no-cache-dir -r requirements-dashboard.txt

COPY poc/ poc/
COPY .streamlit/ .streamlit/

# The client exports are mounted at /data (never baked into the image)
ENV MM_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8502
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8502/_stcore/health')"

CMD ["python", "-m", "streamlit", "run", "poc/p1p2_app.py", "--server.port", "8502", "--server.address", "0.0.0.0"]
