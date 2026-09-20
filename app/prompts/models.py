from dataclasses import dataclass


@dataclass(frozen=True)
class PromptVersion:
    id: str
    description: str
    system_prompt: str
    # Which pipeline stage the prompt is written for. Each stage expects a different output schema, so a
    # prompt must not be run against another stage's schema.
    stage: str
