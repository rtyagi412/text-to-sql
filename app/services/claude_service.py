import json
from dataclasses import dataclass
from functools import lru_cache

import anthropic

from app.core.config import get_settings
from app.schemas.sql_generation import TokenUsage

settings = get_settings()

# The server-side refusal fallback is only offered on these models; other models would reject the parameter.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeRefusalError(RuntimeError):
    """The model (and its fallback, if any) declined the request."""


class ClaudeOutputError(RuntimeError):
    """The response was cut off or carried no usable JSON."""


@dataclass(frozen=True)
class ClaudeJsonResult:
    data: dict
    model: str
    usage: TokenUsage
    request_id: str | None


@lru_cache
def get_client() -> anthropic.Anthropic:
    kwargs: dict = {"timeout": settings.claude_timeout, "max_retries": settings.claude_max_retries}
    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    return anthropic.Anthropic(**kwargs)


def generate_json(
    *,
    system: list[dict],
    user_content: str,
    schema: dict,
    model: str | None = None,
) -> ClaudeJsonResult:
    """One structured-output call: the response is constrained to `schema`, so it is valid JSON by construction.

    Sampling parameters are intentionally not set -- current Claude models reject them. Thinking is left
    at the model default (adaptive), which is where column-selection reasoning happens.
    """
    model = model or settings.claude_model
    output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
    if settings.claude_effort:
        output_config["effort"] = settings.claude_effort

    request: dict = {
        "model": model,
        "max_tokens": settings.claude_max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "output_config": output_config,
    }

    client = get_client()
    if settings.claude_use_fallbacks and model in _FALLBACK_MODELS:
        response = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **request)
    else:
        response = client.messages.create(**request)

    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise ClaudeRefusalError(f"Claude declined the request (category: {category or 'unspecified'})")
    if response.stop_reason == "max_tokens":
        raise ClaudeOutputError(
            f"Response hit max_tokens ({settings.claude_max_tokens}) before the JSON was complete; "
            "raise CLAUDE_MAX_TOKENS"
        )

    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise ClaudeOutputError("Response contained no text block")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudeOutputError(f"Response was not valid JSON: {exc}") from exc

    usage = response.usage
    return ClaudeJsonResult(
        data=data,
        model=response.model,
        usage=TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        ),
        request_id=getattr(response, "_request_id", None),
    )
