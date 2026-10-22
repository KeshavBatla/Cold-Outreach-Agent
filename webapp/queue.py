from __future__ import annotations

import os

from celery import Celery

from webapp.settings import Settings

settings = Settings.from_environment()
celery_app = Celery("cold_outreach", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=max(1, min(int(os.getenv("WORKER_CONCURRENCY", "2")), 8)),
    beat_schedule={
        "dispatch-durable-outbox": {
            "task": "webapp.worker.dispatch_outbox",
            "schedule": 5.0,
        }
    },
)
