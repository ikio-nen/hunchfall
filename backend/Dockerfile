FROM python:3.11-slim

WORKDIR /srv/cascade

# Backend only: paper-trading core + monitor API.
# No secrets are baked in; configure entirely via environment / .env.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/

EXPOSE 8000

# Default: serve the read-only paper monitor API.
# Run the trading loop separately from backend/:
#   python -m app.loop --once    # single cycle
#   python -m app.loop --watch   # every LOOP_INTERVAL_SEC
CMD ["uvicorn", "app.api.run:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
