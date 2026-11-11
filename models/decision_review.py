from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewAssessment(str, Enum):
    NO_ISSUES = "NO_ISSUES"
    ISSUES_FOUND = "ISSUES_FOUND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_DECISIONS = "NO_DECISIONS"


class FindingCategory(str, Enum):
    DATA_CLAIM = "DATA_CLAIM"
    REASONING = "REASONING"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    FOLLOW_UP = "FOLLOW_UP"


class FindingSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: UUID
    category: FindingCategory
    severity: FindingSeverity
    issue: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=700)
    lesson: str = Field(min_length=1, max_length=500)


class DecisionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment: ReviewAssessment
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[ReviewFinding] = Field(max_length=12)
    data_gaps: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def consistent_assessment(self):
        if self.assessment is ReviewAssessment.ISSUES_FOUND and not self.findings:
            raise ValueError("ISSUES_FOUND requires at least one finding")
        if self.assessment in {ReviewAssessment.NO_ISSUES, ReviewAssessment.NO_DECISIONS} and self.findings:
            raise ValueError("Assessment contradicts findings")
        if self.assessment is ReviewAssessment.INSUFFICIENT_DATA and not self.data_gaps:
            raise ValueError("INSUFFICIENT_DATA requires at least one data gap")
        return self


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string", "enum": [item.value for item in ReviewAssessment]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "format": "uuid"},
                "category": {"type": "string", "enum": [item.value for item in FindingCategory]},
                "severity": {"type": "string", "enum": [item.value for item in FindingSeverity]},
                "issue": {"type": "string"},
                "evidence": {"type": "string"},
                "lesson": {"type": "string"},
            },
            "required": ["decision_id", "category", "severity", "issue", "evidence", "lesson"],
            "additionalProperties": False,
        }},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["assessment", "summary", "findings", "data_gaps"],
    "additionalProperties": False,
}
