from app.prompts.models import PromptVersion
from app.prompts.versions.extract_v1 import PROMPT_VERSION as EXTRACT_V1
from app.prompts.versions.mapping_v1 import PROMPT_VERSION as MAPPING_V1
from app.prompts.versions.write_v1 import PROMPT_VERSION as WRITE_V1

_VERSIONS: dict[str, PromptVersion] = {
    EXTRACT_V1.id: EXTRACT_V1,
    MAPPING_V1.id: MAPPING_V1,
    WRITE_V1.id: WRITE_V1,
}


def get_prompt_version(version_id: str, stage: str) -> PromptVersion | None:
    """None when the id is unknown, or belongs to a different stage than the one asked for."""
    version = _VERSIONS.get(version_id)
    return version if version is not None and version.stage == stage else None


def list_prompt_versions(stage: str | None = None) -> list[str]:
    return [v.id for v in _VERSIONS.values() if stage is None or v.stage == stage]
