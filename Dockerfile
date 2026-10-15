# ---- builder: compile deps ----
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install --prefix=/install -r requirements.txt

# ---- runtime: slim, no compilers ----
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# libpq runtime only (psycopg2-binary needs it); no build tools here.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . /app/

# Do not run the Django processes as root. Compose can override this UID/GID
# with the deployment checkout owner so the tracked SQLite database remains
# writable through its host bind mount.
RUN groupadd --system app && useradd --system --gid app --home-dir /home/app \
      --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/media /app/django_cache /app/staticfiles \
    && chown -R app:app /app
USER app

# The CMD will be overridden by compose for different services (see docker-compose.yml)
CMD ["gunicorn", "GroceriesTracker.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
