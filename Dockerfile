FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY timetable_parser ./timetable_parser
COPY assets ./assets
COPY data ./data

EXPOSE 8080

CMD ["sh", "-c", "exec python -m uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
