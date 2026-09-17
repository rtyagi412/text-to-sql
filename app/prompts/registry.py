from app.prompts.models import PromptVersion
from app.prompts.versions.v1 import PROMPT_VERSION as V1
from app.prompts.versions.v2 import PROMPT_VERSION as V2
from app.prompts.versions.v3 import PROMPT_VERSION as V3

_VERSIONS: dict[str, PromptVersion] = {
    V1.id: V1,
    V2.id: V2,
    V3.id: V3,
}


def get_prompt_version(version_id: str) -> PromptVersion | None:
    return _VERSIONS.get(version_id)


def list_prompt_versions() -> list[str]:
    return list(_VERSIONS)
