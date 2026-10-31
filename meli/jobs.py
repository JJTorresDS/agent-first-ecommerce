import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

QUEUE = "vimple:postsale:queue"
ACTIVE_TTL = 30 * 60
DONE_TTL = 2 * 60
BLOCK_SECONDS = 5

_started = False
_start_lock = threading.Lock()


def postsale_queue_enabled():
    return bool(getattr(settings, "REDIS_URL", "").strip())


def status_key(organization_id):
    return f"vimple:postsale:status:{organization_id}"


def _client():
    import redis

    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=BLOCK_SECONDS + 5,
    )


def postsale_status(organization):
    """queued, running, ok, failed, or an empty string when Redis is unused."""
    if organization is None or not postsale_queue_enabled():
        return ""
    try:
        return _client().get(status_key(organization.pk)) or ""
    except Exception:
        logger.exception("Could not read the post-sale sync status")
        return ""


def enqueue_postsale_sync(organization):
    """Add one sync job. A job already queued or running is left as it is."""
    client = _client()
    key = status_key(organization.pk)
    current = client.get(key)
    if current in {"queued", "running"}:
        return current
    client.set(key, "queued", ex=ACTIVE_TTL)
    client.lpush(QUEUE, str(organization.pk))
    return "queued"


def run_next_postsale_job(block_seconds=BLOCK_SECONDS):
    """Run one queued sync. Returns False when the list is empty."""
    item = _client().brpop(QUEUE, timeout=block_seconds)
    if not item:
        return False
    _queue_name, organization_id = item
    _run_job(organization_id)
    return True


def _run_job(organization_id):
    from accounts.models import Organization

    from .postsale import sync_recent_postsale

    client = _client()
    key = status_key(organization_id)
    client.set(key, "running", ex=ACTIVE_TTL)
    close_old_connections()
    try:
        organization = Organization.objects.get(pk=organization_id)
        sync_recent_postsale(organization)
    except Exception:
        logger.exception("Post-sale sync failed for %s", organization_id)
        _client().set(key, "failed", ex=DONE_TTL)
    else:
        _client().set(key, "ok", ex=DONE_TTL)
    finally:
        close_old_connections()


def _worker_loop():
    while True:
        try:
            run_next_postsale_job()
        except Exception:
            logger.exception("Post-sale worker lost the Redis connection")
            close_old_connections()
            time.sleep(BLOCK_SECONDS)


def start_postsale_worker():
    """One daemon thread for the Gunicorn process. Tests and migrate stay quiet."""
    global _started
    if not postsale_queue_enabled() or _skip_worker():
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(
            target=_worker_loop,
            name="postsale-sync",
            daemon=True,
        ).start()
        _started = True


def _skip_worker():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in {"test", "migrate", "makemigrations", "collectstatic", "shell"}:
        return True
    if command == "runserver" and os.environ.get("RUN_MAIN") != "true":
        return True
    return False
