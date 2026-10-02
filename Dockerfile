FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install -r requirements.txt

COPY pam ./pam
COPY config.yaml ./

# One worker only: PlanIt rate-limits per IP. DATABASE_URL comes from the platform.
CMD ["python", "-m", "pam.worker", "run"]
