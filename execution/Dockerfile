# Playwright base image ships Chromium + system deps (for the browser fallback).
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY execution/requirements.txt /app/execution/requirements.txt
RUN pip install --no-cache-dir -r /app/execution/requirements.txt \
    && python -m playwright install chromium

# Execution code + the risk layers + their configs.
COPY execution/ /app/execution/
COPY risk_governor/ /app/risk_governor/
COPY weekly_target_manager/ /app/weekly_target_manager/
COPY risk/risk_logic.py /app/execution/risk_logic.py
COPY trade_pipeline.py /app/trade_pipeline.py
COPY risk_config.json /app/risk_config.json
COPY weekly_target_config.json /app/weekly_target_config.json

# /app for the root packages (risk_governor, weekly_target_manager, trade_pipeline),
# /app/execution for the execution modules.
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/execution
CMD ["python", "-u", "execution/executor.py"]
