"""Step 0, /sql/extract: what the ticket says the report displays and filters on, from the ticket text alone."""

import json
import logging

from app.core.config import get_settings
from app.schemas.sql_generation import LlmCallAudit, RitmExtraction, RitmExtractionResponse
from app.services.ritm_service import get_ritm_by_id
from app.services.sql_generation.common import generate_checked, resolve_version, system_blocks, ticket_sections

settings = get_settings()
logger = logging.getLogger(__name__)


def extract_requirements(
    ritm_number: str,
    user_input: str | None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> RitmExtractionResponse | None:
    """Step 0: what the ticket says the report displays and filters on, read from the ticket text alone.
    No catalog, embeddings or approved examples are involved -- this is the summary the requester
    confirms before the columns are resolved against the schema. Returns None when the RITM does not exist."""
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        return None
    version = resolve_version(prompt_version, settings.extract_prompt_version, "extract")
    extraction, result = generate_checked(
        system=system_blocks(version.system_prompt),
        user_content="\n\n".join(ticket_sections(ritm, user_input)),
        schema=RitmExtraction.generation_json_schema(),
        model=model,
        build=RitmExtraction.model_validate,
    )

    audit = LlmCallAudit(
        prompt_version=version.id,
        model=result.model,
        request_id=result.request_id,
        attempts=result.attempts,
        usage=result.usage,
    )
    status = extraction.derive_status()
    logger.info("requirements extraction %s", json.dumps({"ritm": ritm.number, **audit.model_dump(), "status": status.value}))
    return RitmExtractionResponse(**extraction.model_dump(), ritm_number=ritm.number, status=status, audit=audit)
