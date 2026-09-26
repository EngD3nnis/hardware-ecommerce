# Production image for the Dewmix Django app (web, Celery worker and beat use the same image).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

RUN useradd --create-home --uid 1000 dewmix
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ backend/
# The legacy catalogue + photos are needed by `import_legacy_catalog` and `export_static_catalog`.
COPY dewmix_source/ dewmix_source/

WORKDIR /app/backend
# collectstatic needs no secrets or database: run it with the test settings module.
RUN DJANGO_SETTINGS_MODULE=config.settings.test python manage.py collectstatic --noinput -v0 \
    && mkdir -p media && chown -R dewmix:dewmix /app

USER dewmix
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", \
     "--access-logfile", "-", "--forwarded-allow-ips", "*"]
