from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    model: str = Field(..., description="Ollama model tag, e.g. 'llama3.1:8b'")
    prompt: str
    think: bool = False
    stream: bool = False


class GenerateResponse(BaseModel):
    model: str
    created_at: str
    response: str
    done: bool
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None


class EmbedResponse(BaseModel):
    model: str
    embeddings: list[list[float]]


class GenerateStreamChunk(BaseModel):
    model: str
    created_at: str
    response: str
    done: bool
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None
