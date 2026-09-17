from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.extraction import ExtractionRequest, ExtractionResult
from app.schemas.llm import GenerateRequest, GenerateResponse
from app.services.extraction_service import extract
from app.services.ollama_service import generate, generate_stream

router = APIRouter(prefix="/llm", tags=["llm"])


@router.post("/generate", response_model=None)
def generate_completion(body: GenerateRequest) -> GenerateResponse | StreamingResponse:
    if body.stream:
        return StreamingResponse(
            generate_stream(body.model, body.prompt, think=body.think),
            media_type="application/x-ndjson",
        )
    return generate(body.model, body.prompt, think=body.think)


@router.post("/extract", response_model=ExtractionResult)
def extract_entities(body: ExtractionRequest) -> ExtractionResult:
    try:
        result = extract(body.ritm_number, body.user_input, body.model, body.prompt_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"RITM '{body.ritm_number}' not found")
    return result
