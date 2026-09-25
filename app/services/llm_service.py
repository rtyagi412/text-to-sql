import json
import logging
import re
import time
from dataclasses import dataclass, replace
from functools import lru_cache

import httpx2

from app.core.config import get_settings
from app.schemas.sql_generation import TokenUsage

settings = get_settings()
logger = logging.getLogger(__name__)

_CHAT_PATH = "/chat/completions"
# Overload and rate limiting are worth another attempt; a bad key (401), no balance (402) or a bad request are not.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class LlmConfigError(RuntimeError):
    """The model is called but no DeepSeek API key is configured."""


class LlmApiError(RuntimeError):
    """The DeepSeek API refused the request or could not be reached."""


class LlmRefusalError(RuntimeError):
    """The provider filtered the response."""


class LlmOutputError(RuntimeError):
    """The answer was cut off, empty, or not usable JSON. `raw` is whatever text came back, if any."""

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class LlmJsonResult:
    data: dict
    raw: str  # the answer as text, to send back if it turns out to be unacceptable
    model: str
    usage: TokenUsage
    request_id: str | None
    attempts: int = 1

    def after(self, earlier: "LlmJsonResult | None") -> "LlmJsonResult":
        """This result with the token usage and attempts of `earlier` calls added, so a caller that makes several
        calls can report what all of them cost."""
        if earlier is None:
            return self
        return replace(self, usage=earlier.usage + self.usage, attempts=earlier.attempts + self.attempts)


@lru_cache
def _client() -> httpx2.Client:
    return httpx2.Client(
        base_url=settings.deepseek_base_url,
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        timeout=settings.llm_timeout,
    )


def _skeleton(node: dict, defs: dict) -> object:
    """A placeholder value of the shape a JSON Schema describes. Arrays are left empty: an example item would
    invite the model to invent one (a clarification nobody needs), and the schema already spells out the items."""
    if "$ref" in node:
        return _skeleton(defs[node["$ref"].split("/")[-1]], defs)
    if "allOf" in node:
        return _skeleton(node["allOf"][0], defs)
    if "enum" in node:
        return node["enum"][0]
    options = node.get("anyOf") or node.get("oneOf")
    if options:
        return _skeleton(next((o for o in options if o.get("type") != "null"), options[0]), defs)
    kind = node.get("type")
    if kind == "object":
        return {name: _skeleton(prop, defs) for name, prop in node.get("properties", {}).items()}
    return {"array": [], "string": "...", "integer": 0, "number": 0, "boolean": False}.get(kind)


def _format_instructions(schema: dict) -> str:
    """DeepSeek's JSON mode guarantees syntactically valid JSON only, not a schema, and asks for the word "json"
    and an example in the prompt. The schema and an example of its shape go last in the system prompt: it is
    the same for every request of a stage, so it stays inside the cacheable prefix."""
    example = json.dumps(_skeleton(schema, schema.get("$defs", {})), indent=2)
    return (
        "<response_format>\n"
        "Respond with one JSON object and nothing else: no prose, no markdown fences. It must validate against this "
        "JSON Schema, and every property listed under \"required\" is mandatory:\n"
        f"{json.dumps(schema, separators=(',', ':'))}\n\n"
        "The shape, with arrays shown empty (each item you add must follow the schema):\n"
        f"{example}\n"
        "</response_format>"
    )


def _error_text(response: httpx2.Response) -> str:
    try:
        detail = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = response.text[:300]
    return f"DeepSeek API error {response.status_code}: {detail}"


def _post(body: dict) -> dict:
    for attempt in range(settings.llm_max_retries + 1):
        last = attempt == settings.llm_max_retries
        try:
            response = _client().post(_CHAT_PATH, json=body)
        except httpx2.TransportError as exc:  # timeouts, connection failures
            if last:
                raise LlmApiError(f"DeepSeek API unreachable: {exc}") from exc
        else:
            if response.status_code < 400:
                return response.json()
            if response.status_code not in _RETRY_STATUSES or last:
                raise LlmApiError(_error_text(response))
        time.sleep(min(2**attempt, 8))
    raise LlmApiError("DeepSeek API request failed")  # unreachable: the last attempt returns or raises


def _parse_json(content: str) -> dict:
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmOutputError(f"Response was not valid JSON: {exc}", raw=content) from exc
    if not isinstance(data, dict):
        raise LlmOutputError("Response was JSON but not an object", raw=content)
    return data


def generate_json(
    *,
    system: str,
    user_content: str,
    schema: dict,
    model: str | None = None,
    prior_answer: str | None = None,
    feedback: str | None = None,
) -> LlmJsonResult:
    """One JSON-mode call whose answer is meant to fit `schema`. The API cannot enforce the schema, so the caller
    validates the answer and can send a rejected one back: `prior_answer` (the text it returned) and `feedback`
    (why it was rejected) are appended to the conversation and the model is asked for a corrected one.

    Thinking is off by default and the temperature is fixed: JSON mode with thinking is not documented, temperature
    has no effect while thinking, and the tasks are deterministic. Both are settings."""
    if not settings.deepseek_api_key:
        raise LlmConfigError("DEEPSEEK_API_KEY is not set; it is required for the extract, map and write stages")
    model = model or settings.llm_model

    messages = [
        {"role": "system", "content": f"{system}\n\n{_format_instructions(schema)}"},
        {"role": "user", "content": user_content},
    ]
    if prior_answer and feedback:
        messages += [
            {"role": "assistant", "content": prior_answer},
            {"role": "user", "content": f"That answer was rejected: {feedback}\n\nReturn the corrected JSON object in full."},
        ]

    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": settings.llm_max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "enabled" if settings.llm_thinking else "disabled"},
    }
    if settings.llm_thinking:
        if settings.llm_reasoning_effort:
            body["reasoning_effort"] = settings.llm_reasoning_effort
    else:
        body["temperature"] = settings.llm_temperature

    payload = _post(body)
    choice = (payload.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    content = (choice.get("message") or {}).get("content") or ""

    if finish == "content_filter":
        raise LlmRefusalError("The response was withheld by the provider's content filter")
    if finish == "length":
        raise LlmOutputError(
            f"Response hit max_tokens ({settings.llm_max_tokens}) before the JSON was complete; raise LLM_MAX_TOKENS",
            raw=content,
        )
    if finish in ("insufficient_system_resource", "aborted"):
        raise LlmOutputError(f"The provider interrupted the response ({finish})", raw=content)
    if not content.strip():
        # JSON mode can occasionally return nothing (the provider documents it), so this is retried, not fatal.
        raise LlmOutputError("Response was empty")
    data = _parse_json(content)

    usage = payload.get("usage") or {}
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    return LlmJsonResult(
        data=data,
        raw=content,
        model=payload.get("model") or model,
        usage=TokenUsage(
            input_tokens=miss if miss is not None else max((usage.get("prompt_tokens") or 0) - hit, 0),
            output_tokens=usage.get("completion_tokens") or 0,
            cache_read_input_tokens=hit,
        ),
        request_id=payload.get("id"),
    )
