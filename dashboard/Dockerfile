FROM python:3.11-slim

WORKDIR /app/dashboard

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install --no-cache-dir -r /app/dashboard/requirements.txt

COPY dashboard/ /app/dashboard/

ENV PYTHONUNBUFFERED=1
EXPOSE 8050
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8050"]
