FROM python:3.11-slim

WORKDIR /app

# System deps kept minimal; psycopg[binary] ships its own libpq.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY research/ /app/research/

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/research
CMD ["python", "-u", "research/sidecar.py"]
