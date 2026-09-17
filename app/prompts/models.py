from dataclasses import dataclass, field


@dataclass(frozen=True)
class FewShotExample:
    input: str
    output: str


@dataclass(frozen=True)
class PromptVersion:
    id: str
    description: str
    system_prompt: str
    few_shot_examples: list[FewShotExample] = field(default_factory=list)
