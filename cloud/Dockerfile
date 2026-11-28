FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 pb && useradd -u 10001 -g 10001 -M pb && mkdir -p /data && chown 10001:10001 /data
COPY pb_bot ./pb_bot
COPY pb_telegram.py pb_market.py pb_service.py paper_runner.py cloud_start.py ./
USER 10001:10001
CMD ["python", "-u", "cloud_start.py"]
