import json

from app.core.config import get_settings
from app.prompts.models import FewShotExample
from app.prompts.registry import get_prompt_version
from app.schemas.extraction import ExtractionResult, RitmExtractionData, Status
from app.schemas.ritm import Ritm
from app.services.guardrails import apply_ambiguity_guardrails
from app.services.ollama_service import generate
from app.services.ritm_service import get_ritm_by_id

settings = get_settings()


def _ritm_context(ritm: Ritm) -> dict:
    variables = ritm.variables
    return {
        "ritm_number": ritm.number,
        "output_fields": variables.output_fields,
        "report_criteria": variables.report_criteria,
        "summary": variables.summary,
    }


def _build_user_prompt(ritm: Ritm, user_input: str | None, few_shot_examples: list[FewShotExample]) -> str:
    sections = []
    for i, example in enumerate(few_shot_examples, start=1):
        sections.append(f"Example {i}:\nInput:\n{example.input}\nOutput:\n{example.output}")

    sections.append("Now extract from the following RITM data:")
    sections.append("RITM data:")
    sections.append(json.dumps(_ritm_context(ritm), indent=2))
    if user_input:
        sections.append("Additional clarification from user:")
        sections.append(user_input)
    return "\n\n".join(sections)


def extract_for_ritm(
    ritm: Ritm,
    user_input: str | None,
    model: str,
    prompt_version: str | None = None,
) -> ExtractionResult:
    version_id = prompt_version or settings.extraction_prompt_version
    version = get_prompt_version(version_id)
    if version is None:
        raise ValueError(f"Unknown prompt version '{version_id}'")

    prompt = _build_user_prompt(ritm, user_input, version.few_shot_examples)
    response = generate(
        model,
        prompt,
        system=version.system_prompt,
        format=RitmExtractionData.generation_json_schema(),
        think=False,
    )
    raw = json.loads(response.response)
    # The model's own status/ambiguities coherence isn't trustworthy: normalize status
    # from ambiguities here (status is always derived, never taken as-is from the LLM)
    # so a self-contradictory response can't fail validation before guardrails can run.
    raw["status"] = Status.NEEDS_CLARIFICATION.value if raw.get("ambiguities") else Status.READY.value
    data = RitmExtractionData.model_validate(raw)
    data = apply_ambiguity_guardrails(ritm, data)
    return ExtractionResult(**data.model_dump(), prompt_version=version.id)


def extract(
    ritm_number: str,
    user_input: str | None,
    model: str,
    prompt_version: str | None = None,
) -> ExtractionResult | None:
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        return None
    return extract_for_ritm(ritm, user_input, model, prompt_version)
