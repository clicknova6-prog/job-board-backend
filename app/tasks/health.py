"""Minimal task used to verify Celery and Redis connectivity."""

from app.celery_app import celery_app


@celery_app.task(name="app.tasks.health.ping")
def ping() -> str:
    """Return a static response for an end-to-end worker health check."""
    return "pong"
