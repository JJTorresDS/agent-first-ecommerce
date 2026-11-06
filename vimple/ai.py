"""One call to Mistral for a suggestion the user reviews before saving. Never sends tokens or costs.

The key is MISTRAL_API_KEY in the environment; without it, enabled() is False and the AI buttons stay hidden.
"""

import json
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)

URL = "https://api.mistral.ai/v1/chat/completions"
TIMEOUT = 25


class SuggestionFailed(Exception):
    """Mistral didn't answer, or not with a JSON object. The reason is for the log, not the page."""


def enabled():
    return bool(getattr(settings, "MISTRAL_API_KEY", ""))


def complete_json(system, user):
    """Mistral's answer as a dict. Raises SuggestionFailed."""
    if not enabled():
        raise SuggestionFailed("MISTRAL_API_KEY is not set")
    body = {
        "model": settings.MISTRAL_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }
    request = Request(
        URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {settings.MISTRAL_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            answer = json.loads(response.read().decode())
        content = answer["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except HTTPError as error:
        logger.warning("Mistral refused the call: HTTP %s", error.code)
        raise SuggestionFailed(f"HTTP {error.code}") from None
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("Mistral call failed: %s", error.__class__.__name__)
        raise SuggestionFailed(error.__class__.__name__) from None
    if not isinstance(parsed, dict):
        raise SuggestionFailed("not a JSON object")
    return parsed
