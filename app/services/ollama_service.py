import json
from collections.abc import Iterator

import httpx2

from app.core.config import get_settings
from app.schemas.llm import EmbedResponse, GenerateResponse, GenerateStreamChunk

settings = get_settings()

_GENERATE_PATH = "/api/generate"
_EMBED_PATH = "/api/embed"


def generate(
    model: str,
    prompt: str,
    system: str | None = None,
    format: dict | None = None,
    think: bool = False,
    temperature: float = 0.0,
    seed: int | None = 42,
) -> GenerateResponse:
    payload = {
        "model": model,
        "prompt": prompt,
        "think": think,
        "stream": False,
        "options": {"temperature": temperature, "top_k": 1, "seed": seed},
    }
    if system is not None:
        payload["system"] = system
    if format is not None:
        payload["format"] = format
    with httpx2.Client(base_url=settings.ollama_base_url, timeout=settings.ollama_request_timeout) as client:
        response = client.post(_GENERATE_PATH, json=payload)
        response.raise_for_status()
        return GenerateResponse.model_validate(response.json())


def embed(model: str, input: str | list[str]) -> EmbedResponse:
    payload = {"model": model, "input": input}
    with httpx2.Client(base_url=settings.ollama_base_url, timeout=settings.ollama_request_timeout) as client:
        response = client.post(_EMBED_PATH, json=payload)
        response.raise_for_status()
        return EmbedResponse.model_validate(response.json())


def generate_stream(model: str, prompt: str, think: bool = False) -> Iterator[str]:
    payload = {"model": model, "prompt": prompt, "think": think, "stream": True}
    with httpx2.Client(base_url=settings.ollama_base_url, timeout=settings.ollama_request_timeout) as client:
        with client.stream("POST", _GENERATE_PATH, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = GenerateStreamChunk.model_validate(json.loads(line))
                yield chunk.model_dump_json() + "\n"
