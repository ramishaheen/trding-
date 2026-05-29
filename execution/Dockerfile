# Playwright base image ships Chromium + system deps.
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY execution/requirements.txt /app/execution/requirements.txt
RUN pip install --no-cache-dir -r /app/execution/requirements.txt \
    && python -m playwright install chromium

# risk_logic is reused by the live watchdog.
COPY execution/ /app/execution/
COPY risk/risk_logic.py /app/execution/risk_logic.py

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/execution
# Default command is overridden per-service in docker-compose.
CMD ["python", "-u", "execution/bridge.py"]
