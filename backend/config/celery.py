"""Celery application for the Dewmix platform.

Run a worker from the backend/ directory with:

    celery -A config worker -l info

DJANGO_SETTINGS_MODULE must be set explicitly for workers (see config/settings/__init__.py).
"""

import os

from celery import Celery
from celery.signals import before_task_publish, task_postrun, task_prerun

from apps.core.request_context import CORRELATION_HEADER, get_correlation_id, set_correlation_id

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

app = Celery("dewmix")

# All Celery settings live in Django settings under the CELERY_ prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@before_task_publish.connect
def _attach_correlation_id(headers=None, **kwargs):
    """Carry the current request's correlation id into the task message."""
    correlation_id = get_correlation_id()
    if headers is not None and correlation_id:
        headers.setdefault(CORRELATION_HEADER, correlation_id)


@task_prerun.connect
def _restore_correlation_id(task=None, **kwargs):
    """Make the publisher's correlation id available to logs inside the task."""
    correlation_id = getattr(task.request, CORRELATION_HEADER, None) if task else None
    set_correlation_id(correlation_id or (task.request.id if task else None))


@task_postrun.connect
def _clear_correlation_id(**kwargs):
    set_correlation_id(None)
