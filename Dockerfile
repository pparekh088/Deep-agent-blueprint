# One image, two entrypoints (API + worker) — deployed as two AKS Deployments.
#   API:    docker run image                       (default CMD: uvicorn)
#   Worker: docker run image arq app.worker.main.WorkerSettings
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Non-root user — required by the AKS pod security baseline.
RUN groupadd -r agent && useradd -r -g agent -d /srv agent
USER agent

EXPOSE 8000

# API entrypoint (worker deployments override CMD).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
