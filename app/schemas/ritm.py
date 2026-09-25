from pydantic import BaseModel, ConfigDict, Field


class ReportVariables(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_fields: str
    report_criteria: str
    description: str
    report_usage: str


class Ritm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(..., pattern=r"^RITM[0-9]+$")
    name: str = Field(..., min_length=1)
    variables: ReportVariables
