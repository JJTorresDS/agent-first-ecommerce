import json
import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import close_old_connections

from .client import describe_failure

logger = logging.getLogger(__name__)



def _key(*parts):
    """A Redis key under this environment's prefix, so local and Render never share jobs."""
    return ":".join([settings.REDIS_PREFIX, *[str(part) for part in parts]])


QUEUE = _key("postsale", "queue")
REPLY_QUEUE = _key("reply", "queue")
REFRESH_QUEUE = _key("refresh", "queue")
NOTIFICATION_QUEUE = _key("notification", "queue")
LISTINGS_QUEUE = _key("listings", "queue")
SALES_QUEUE = _key("sales", "queue")
NOTIFICATION_TOPICS = {"questions", "messages", "items", "orders_v2"}
ACTIVE_TTL = 30 * 60
DONE_TTL = 2 * 60
ERROR_TTL = 10 * 60
REFRESH_COOLDOWN = 60
REFRESH_LIMIT = 20
BLOCK_SECONDS = 5
_local_reply_status = {}
_local_reply_error = {}
_local_refresh = {}

_clients = {}
_client_lock = threading.Lock()

_started = False
_start_lock = threading.Lock()


def postsale_queue_enabled():
    return bool(getattr(settings, "REDIS_URL", "").strip())


def status_key(organization_id):
    return _key("postsale", "status", organization_id)


def reply_status_key(conversation_id):
    return _key("reply", "status", conversation_id)


def reply_error_key(conversation_id):
    return _key("reply", "error", conversation_id)


def refresh_status_key(organization_id):
    return _key("refresh", "status", organization_id)


def _client():
    """One client per URL, so its connection pool is reused across calls and threads."""
    import redis

    url = settings.REDIS_URL
    with _client_lock:
        client = _clients.get(url)
        if client is None:
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=BLOCK_SECONDS + 5,
            )
            _clients[url] = client
    return client


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
    return run_next_job(block_seconds, queues=(QUEUE,))


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


def reply_error(conversation):
    """{"message", "text"} for the last reply the channel refused, or None."""
    if conversation is None:
        return None
    if postsale_queue_enabled():
        try:
            raw = _client().get(reply_error_key(conversation.pk))
        except Exception:
            logger.exception("Could not read the reply error")
            return None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None
    return _local_reply_error.get(str(conversation.pk))


def _set_reply_error(conversation_id, message, text):
    error = {"message": message or "", "text": text or ""}
    if postsale_queue_enabled():
        _client().set(reply_error_key(conversation_id), json.dumps(error), ex=ERROR_TTL)
        return
    _local_reply_error[str(conversation_id)] = error


def clear_reply_error(conversation):
    if postsale_queue_enabled():
        try:
            _client().delete(reply_error_key(conversation.pk))
        except Exception:
            logger.exception("Could not clear the reply error")
        return
    _local_reply_error.pop(str(conversation.pk), None)


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
                "queued_at": time.time(),
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
    return run_next_job(block_seconds, queues=(REPLY_QUEUE,))


def _run_reply_payload(payload):
    try:
        job = json.loads(payload)
    except ValueError:
        logger.exception("Reply queue item was not JSON")
        return
    run_reply_job(job.get("organization_id"), job.get("conversation_id"), job.get("queued_at"))


def run_reply_job(organization_id, conversation_id, queued_at=None):
    """Send one queued reply. Logs how long it waited and how long the channel took."""
    started = time.time()
    try:
        _send_reply(organization_id, conversation_id)
    finally:
        waited = started - queued_at if isinstance(queued_at, (int, float)) else 0.0
        logger.info(
            "Reply %s sent in %.1f s after waiting %.1f s",
            conversation_id,
            time.time() - started,
            max(0.0, waited),
        )


def _send_reply(organization_id, conversation_id):
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
        except ReplyFailed as error:
            _drop_pending(conversation.pk)
            _set_reply_error(conversation_id, error.message, pending.body)
            _set_reply_status(conversation_id, "failed")
        else:
            if Message.objects.filter(
                conversation_id=conversation.pk,
                external_id="pending",
            ).exists():
                _drop_pending(conversation.pk)
                _set_reply_error(conversation_id, "", pending.body)
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
    return run_next_job(block_seconds, queues=(REFRESH_QUEUE,))


def _run_refresh(organization_id):
    key = refresh_status_key(organization_id)
    client = _client()
    if client.get(key) in {"ok", "failed"}:
        # A duplicate queued before the last refresh finished.
        return
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
        except (OSError, ValueError, InvalidToken) as error:
            logger.warning(
                "Could not refresh %s %s for %s: %s",
                conversation.channel,
                conversation.external_id,
                organization.pk,
                describe_failure(error),
            )
            continue
        except Exception:
            logger.exception(
                "Could not refresh %s %s",
                conversation.channel,
                conversation.external_id,
            )
    from .postsale import refresh_postsale

    try:
        refresh_postsale(organization)
    except (OSError, ValueError, InvalidToken) as error:
        logger.warning(
            "Post-sale catch-up failed for %s: %s",
            organization_id,
            describe_failure(error),
        )
    except Exception:
        logger.exception("Post-sale catch-up failed for %s", organization_id)


def dispatch_notification(parsed):
    """Load what a webhook points at without making Mercado Libre wait.

    manage.py test runs it inline. Without Redis a daemon thread runs it.
    If Redis fails the load runs inline, so the message is not lost.
    """
    if not isinstance(parsed, dict) or parsed.get("topic") not in NOTIFICATION_TOPICS:
        return
    job = {
        "topic": parsed.get("topic"),
        "resource": parsed.get("resource"),
        "user_id": parsed.get("user_id"),
    }
    if _under_test():
        run_notification_job(job)
        return
    if postsale_queue_enabled():
        try:
            _client().lpush(NOTIFICATION_QUEUE, json.dumps(job))
            return
        except Exception:
            logger.exception("Could not queue the notification")
        run_notification_job(job)
        return
    threading.Thread(
        target=run_notification_job,
        args=(job,),
        name="meli-notification",
        daemon=True,
    ).start()


def run_next_notification_job(block_seconds=BLOCK_SECONDS):
    """Run one queued notification. Returns False when the list is empty."""
    return run_next_job(block_seconds, queues=(NOTIFICATION_QUEUE,))


def _run_notification_payload(payload):
    try:
        job = json.loads(payload)
    except ValueError:
        logger.exception("Notification queue item was not JSON")
        return
    run_notification_job(job)


def run_notification_job(job):
    from cryptography.fernet import InvalidToken

    from .postsale import load_postsale_from_notification
    from .views import _load_question_from_notification

    close_old_connections()
    try:
        if job.get("topic") == "questions":
            _load_question_from_notification(job)
        elif job.get("topic") == "messages":
            load_postsale_from_notification(job)
        elif job.get("topic") == "items":
            _refresh_listing_from_notification(job)
        elif job.get("topic") == "orders_v2":
            from .sales import load_order_from_notification

            load_order_from_notification(job)
    except (OSError, ValueError, InvalidToken) as error:
        logger.warning(
            "Notification %s could not be loaded: %s",
            job.get("resource"),
            describe_failure(error),
        )
    except Exception:
        logger.exception("Notification load failed for %s", job.get("resource"))
    finally:
        close_old_connections()


def _refresh_listing_from_notification(job):
    """An items webhook: re-read that one listing for every organization with this seller."""
    import re

    from cryptography.fernet import InvalidToken

    from .listings import refresh_listing
    from .views import _clip, _organizations_for

    match = re.fullmatch(r"/items/([A-Z]{3}\d+)", str(job.get("resource") or ""))
    if not match:
        return
    for organization in _organizations_for(_clip(job.get("user_id"), 64)):
        try:
            refresh_listing(organization, match.group(1))
        except (OSError, ValueError, InvalidToken) as error:
            logger.warning(
                "Listing %s could not be refreshed for %s: %s",
                match.group(1),
                organization.pk,
                describe_failure(error),
            )


def dispatch_listing_sync(organization):
    """Queue a full listing sync. One already queued or running is left as it is."""
    from .models import MeliListingSync

    record, _ = MeliListingSync.objects.get_or_create(organization=organization)
    if record.busy():
        return record
    record.state = MeliListingSync.State.QUEUED
    record.error = ""
    record.save(update_fields=["state", "error", "updated_at"])
    if _under_test():
        _run_listing_sync(str(organization.pk))
    elif postsale_queue_enabled():
        try:
            _client().lpush(LISTINGS_QUEUE, str(organization.pk))
        except Exception:
            logger.exception("Could not queue the listing sync")
            record.state = MeliListingSync.State.FAILED
            record.error = "The sync could not be queued. Try again."
            record.save(update_fields=["state", "error", "updated_at"])
    else:
        threading.Thread(
            target=_run_listing_sync,
            args=(str(organization.pk),),
            name="meli-listings",
            daemon=True,
        ).start()
    record.refresh_from_db()
    return record


def _run_listing_sync(organization_id):
    from accounts.models import Organization

    from .listings import sync_listings

    close_old_connections()
    try:
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is not None:
            sync_listings(organization)
    except Exception:
        logger.exception("Listing sync failed for %s", organization_id)
    finally:
        close_old_connections()


def dispatch_sales_sync(organization):
    """Queue the sales catch-up. One already queued or running is left as it is."""
    from .models import MeliListingSync, MeliSalesSync

    record, _ = MeliSalesSync.objects.get_or_create(organization=organization)
    if record.busy():
        return record
    record.state = MeliListingSync.State.QUEUED
    record.error = ""
    record.save(update_fields=["state", "error", "updated_at"])
    if _under_test():
        _run_sales_sync(str(organization.pk))
    elif postsale_queue_enabled():
        try:
            _client().lpush(SALES_QUEUE, str(organization.pk))
        except Exception:
            logger.exception("Could not queue the sales sync")
            record.state = MeliListingSync.State.FAILED
            record.error = "The sync could not be queued. Try again."
            record.save(update_fields=["state", "error", "updated_at"])
    else:
        threading.Thread(target=_run_sales_sync, args=(str(organization.pk),), name="meli-sales", daemon=True).start()
    record.refresh_from_db()
    return record


def _run_sales_sync(organization_id):
    from accounts.models import Organization

    from .sales import sync_sales

    close_old_connections()
    try:
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is not None:
            sync_sales(organization)
    except Exception:
        logger.exception("Sales sync failed for %s", organization_id)
    finally:
        close_old_connections()


# Replies have their own thread (REPLY_LANE): a person is waiting, and a sync or refresh
# can take tens of seconds. The other thread takes the rest; BRPOP takes the first non-empty list.
REPLY_LANE = (REPLY_QUEUE,)
WORKER_QUEUES = (NOTIFICATION_QUEUE, QUEUE, LISTINGS_QUEUE, SALES_QUEUE, REFRESH_QUEUE)


def run_next_job(block_seconds=BLOCK_SECONDS, queues=WORKER_QUEUES):
    """Run one job from the first of these queues that has one.

    Waits up to block_seconds. Redis reads a timeout of 0 as wait forever, so
    the wait is always at least one second. Returns False when nothing came.
    """
    item = _client().brpop(list(queues), timeout=max(1, int(block_seconds)))
    if not item:
        return False
    queue, value = item
    handlers = {
        REPLY_QUEUE: _run_reply_payload,
        NOTIFICATION_QUEUE: _run_notification_payload,
        QUEUE: _run_job,
        LISTINGS_QUEUE: _run_listing_sync,
        SALES_QUEUE: _run_sales_sync,
        REFRESH_QUEUE: _run_refresh,
    }
    handlers[queue](value)
    return True


def _worker_loop(queues=WORKER_QUEUES):
    while True:
        try:
            run_next_job(queues=queues)
        except Exception:
            logger.exception("Post-sale worker lost the Redis connection")
            close_old_connections()
            time.sleep(BLOCK_SECONDS)


def start_postsale_worker():
    """Two daemon threads for the Gunicorn process: replies alone, and everything else.

    Tests and migrate stay quiet.
    """
    global _started
    if not postsale_queue_enabled() or _skip_worker():
        return
    with _start_lock:
        if _started:
            return
        for name, queues in (("meli-replies", REPLY_LANE), ("meli-jobs", WORKER_QUEUES)):
            threading.Thread(target=_worker_loop, args=(queues,), name=name, daemon=True).start()
        _started = True


def _skip_worker():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in {"test", "migrate", "makemigrations", "collectstatic", "shell"}:
        return True
    if command == "runserver" and os.environ.get("RUN_MAIN") != "true":
        return True
    return False
