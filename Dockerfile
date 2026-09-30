FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data CONFIG_PATH=/app/config/universe.json
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY marketdata ./marketdata
COPY config ./config
USER app
EXPOSE 8000
CMD ["uvicorn", "marketdata.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
