FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV CHIEF_DB_PATH=/data/chief.db \
    CHIEF_CHROMA_PATH=/data/chroma \
    SUPERVISOR_WORKSPACE_ROOT=/workspaces \
    SUPERVISOR_LOG_ROOT=/data/logs

VOLUME ["/data", "/workspaces"]

EXPOSE 8000

CMD ["python3", "main.py"]
