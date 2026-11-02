import json
import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

QUEUE = "vimple:postsale:queue"
REPLY_QUEUE = "vimple:reply:queue"
REFRESH_QUEUE = "vimple:refresh:queue"
ACTIVE_TTL = 30 * 60
DONE_TTL = 2 * 60
REFRESH_COOLDOWN = 60
REFRESH_LIMIT = 20
BLOCK_SECONDS = 5
_local_reply_status = {}
_local_refresh = {}

_started = False
_start_lock = threading.Lock()


def postsale_queue_enabled():
    return bool(getattr(settings, "REDIS_URL", "").strip())


def status_key(organization_id):
    return f"vimple:postsale:status:{organization_id}"


def reply_status_key(conversation_id):
    return f"vimple:reply:status:{conversation_id}"


def refresh_status_key(organization_id):
    return f"vimple:refresh:status:{organization_id}"


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


def reply_status(conversation):
    """queued, running, ok, failed, or an empty string when no reply is in flight."""
    if conversation is None:
        return ""
    if postsale_queue_enabled():
        try:
            return _client().get(reply_status_key(conversation.pk)) or ""
        except Exception:
            logger.exception("Could not read the reply status")
            return ""
    return _local_reply_status.get(str(conversation.pk), "")


def _set_reply_status(conversation_id, value):
    ttl = ACTIVE_TTL if value in {"queued", "running"} else DONE_TTL
    if postsale_queue_enabled():
        _client().set(reply_status_key(conversation_id), value, ex=ttl)
        return
    _local_reply_status[str(conversation_id)] = value


def enqueue_reply(conversation):
    """Queue one reply. A reply already queued or running is left as it is."""
    client = _client()
    key = reply_status_key(conversation.pk)
    current = client.get(key)
    if current in {"queued", "running"}:
        return current
    client.set(key, "queued", ex=ACTIVE_TTL)
    client.lpush(
        REPLY_QUEUE,
        json.dumps(
            {
                "organization_id": str(conversation.organization_id),
                "conversation_id": str(conversation.pk),
            }
        ),
    )
    return "queued"


def dispatch_reply(conversation):
    """Queue the reply, or run it now when Redis is unset.

    manage.py test runs it inline so the existing reply tests stay deterministic.
    A local server without Redis starts a daemon thread and returns immediately.
    """
    if postsale_queue_enabled():
        try:
            return enqueue_reply(conversation)
        except Exception:
            logger.exception("Could not queue the reply")
            _drop_pending(conversation.pk)
            return "failed"
    if _under_test():
        run_reply_job(conversation.organization_id, conversation.pk)
        return reply_status(conversation) or "ok"
    _set_reply_status(conversation.pk, "queued")
    threading.Thread(
        target=run_reply_job,
        args=(str(conversation.organization_id), str(conversation.pk)),
        name="inbox-reply",
        daemon=True,
    ).start()
    return "queued"


def run_next_reply_job(block_seconds=BLOCK_SECONDS):
    """Run one queued reply. Returns False when the list is empty."""
    item = _client().brpop(REPLY_QUEUE, timeout=block_seconds)
    if not item:
        return False
    _queue_name, payload = item
    try:
        job = json.loads(payload)
    except ValueError:
        logger.exception("Reply queue item was not JSON")
        return True
    run_reply_job(job.get("organization_id"), job.get("conversation_id"))
    return True


def run_reply_job(organization_id, conversation_id):
    from inbox.channels import ReplyFailed, sender_for
    from inbox.models import Conversation, Message

    close_old_connections()
    try:
        conversation = Conversation.objects.filter(
            organization_id=organization_id,
            pk=conversation_id,
        ).first()
        if conversation is None:
            return
        _set_reply_status(conversation_id, "running")
        pending = conversation.messages.filter(external_id="pending").first()
        if pending is None:
            _set_reply_status(
                conversation_id,
                "ok" if not conversation.needs_reply else "failed",
            )
            return
        send = sender_for(conversation.channel)
        try:
            if send is not None:
                send(conversation, pending.body)
        except ReplyFailed:
            _drop_pending(conversation.pk)
            _set_reply_status(conversation_id, "failed")
        else:
            if Message.objects.filter(
                conversation_id=conversation.pk,
                external_id="pending",
            ).exists():
                _drop_pending(conversation.pk)
                _set_reply_status(conversation_id, "failed")
            else:
                _set_reply_status(conversation_id, "ok")
    except Exception:
        logger.exception("Reply failed for %s", conversation_id)
        _drop_pending(conversation_id)
        _set_reply_status(conversation_id, "failed")
    finally:
        close_old_connections()


def _drop_pending(conversation_id):
    from inbox.models import Message

    Message.objects.filter(
        conversation_id=conversation_id,
        external_id="pending",
    ).delete()


def _under_test():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    return command == "test"


def enqueue_refresh(organization):
    """Queue one refresh. A recent or in-flight refresh is left as it is."""
    client = _client()
    key = refresh_status_key(organization.pk)
    current = client.get(key)
    if current in {"queued", "running", "ok", "failed"}:
        return current
    client.set(key, "queued", ex=ACTIVE_TTL)
    client.lpush(REFRESH_QUEUE, str(organization.pk))
    return "queued"


def dispatch_refresh(organization):
    """Queue a refresh of threads that still need a reply.

    manage.py test does not refresh, so opening the inbox stays a database read.
    A local server without Redis starts a daemon thread and returns immediately.
    """
    if organization is None or _under_test():
        return
    if postsale_queue_enabled():
        try:
            enqueue_refresh(organization)
        except Exception:
            logger.exception("Could not queue the unanswered refresh")
        return
    key = str(organization.pk)
    if _refresh_blocked(key):
        return
    _local_refresh[key] = ("active",)
    threading.Thread(
        target=_refresh_in_thread,
        args=(key,),
        name="inbox-refresh",
        daemon=True,
    ).start()


def _refresh_blocked(organization_id):
    state = _local_refresh.get(str(organization_id))
    if not state:
        return False
    if state[0] == "active":
        return True
    return time.monotonic() < state[1]


def _refresh_in_thread(organization_id):
    close_old_connections()
    try:
        refresh_unanswered(organization_id)
    except Exception:
        logger.exception("Unanswered refresh failed for %s", organization_id)
    finally:
        _local_refresh[str(organization_id)] = (
            "done",
            time.monotonic() + REFRESH_COOLDOWN,
        )
        close_old_connections()


def run_next_refresh_job(block_seconds=BLOCK_SECONDS):
    """Run one queued refresh. Returns False when the list is empty."""
    item = _client().brpop(REFRESH_QUEUE, timeout=block_seconds)
    if not item:
        return False
    _queue_name, organization_id = item
    _run_refresh(organization_id)
    return True


def _run_refresh(organization_id):
    key = refresh_status_key(organization_id)
    client = _client()
    client.set(key, "running", ex=ACTIVE_TTL)
    close_old_connections()
    try:
        refresh_unanswered(organization_id)
    except Exception:
        logger.exception("Unanswered refresh failed for %s", organization_id)
        _client().set(key, "failed", ex=REFRESH_COOLDOWN)
    else:
        _client().set(key, "ok", ex=REFRESH_COOLDOWN)
    finally:
        close_old_connections()


def refresh_unanswered(organization_id):
    """Re-fetch up to 20 threads that still need a reply for one organization."""
    from accounts.models import Organization
    from cryptography.fernet import InvalidToken

    from inbox.models import Channel, Conversation

    organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        return
    conversations = Conversation.objects.filter(
        organization=organization,
        needs_reply=True,
    ).order_by("-last_message_at", "-created_at")[:REFRESH_LIMIT]
    for conversation in conversations:
        try:
            if conversation.channel == Channel.MELI_ITEM_QUESTIONS:
                from .views import _load_question

                _load_question(organization, conversation.external_id)
            elif conversation.channel == Channel.MELI_POSTSALES:
                from .postsale import load_pack

                load_pack(organization, conversation.external_id)
        except (OSError, ValueError, InvalidToken):
            continue
        except Exception:
            logger.exception(
                "Could not refresh %s %s",
                conversation.channel,
                conversation.external_id,
            )


def _worker_loop():
    while True:
        try:
            run_next_postsale_job()
            run_next_reply_job(block_seconds=0)
            run_next_refresh_job(block_seconds=0)
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
