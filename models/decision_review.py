from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewAssessment(str, Enum):
    NO_ISSUES = "NO_ISSUES"
    ISSUES_FOUND = "ISSUES_FOUND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_DECISIONS = "NO_DECISIONS"
    NO_ACTIONS = "NO_ACTIONS"


class ActionVerdict(str, Enum):
    JUSTIFIED = "JUSTIFIED"
    QUESTIONABLE = "QUESTIONABLE"
    ERROR = "ERROR"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ActionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=120)
    action_type: str = Field(min_length=1, max_length=20)
    verdict: ActionVerdict
    reason: str = Field(min_length=1, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    lesson: str = Field(max_length=400)


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
    action_id: str = Field(min_length=1, max_length=120)
    category: FindingCategory
    severity: FindingSeverity
    claim: str = Field(max_length=300)
    issue: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=700)
    lesson: str = Field(min_length=1, max_length=500)


class DecisionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment: ReviewAssessment
    summary: str = Field(min_length=1, max_length=1000)
    action_reviews: list[ActionReview] = Field(default_factory=list, max_length=500)
    findings: list[ReviewFinding] = Field(max_length=200)
    data_gaps: list[str] = Field(max_length=200)

    @model_validator(mode="after")
    def consistent_assessment(self):
        if self.assessment is ReviewAssessment.ISSUES_FOUND and not self.findings:
            raise ValueError("ISSUES_FOUND requires a demonstrated error finding")
        if self.assessment in {ReviewAssessment.NO_ISSUES, ReviewAssessment.NO_DECISIONS, ReviewAssessment.NO_ACTIONS} and self.findings:
            raise ValueError("Assessment contradicts findings")
        if self.assessment is ReviewAssessment.NO_ISSUES and any(
            item.verdict is ActionVerdict.ERROR for item in self.action_reviews
        ):
            raise ValueError("NO_ISSUES contradicts action errors")
        if self.assessment is ReviewAssessment.INSUFFICIENT_DATA and not self.data_gaps:
            raise ValueError("INSUFFICIENT_DATA requires at least one data gap")
        return self


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string", "enum": [item.value for item in ReviewAssessment]},
        "summary": {"type": "string"},
        "action_reviews": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
            "action_type": {"type": "string", "enum": ["OPEN_LONG", "OPEN_SHORT", "CLOSE", "HOLD", "AUTO_CLOSE",
                                                     "ORDER_CREATE", "ORDER_AMEND", "ORDER_CANCEL", "ORDER_HOLD",
                                                     "ORDER_FILL", "ORDER_EXPIRE", "ORDER_REJECT", "ORDER_MISSED"]},
                "verdict": {"type": "string", "enum": [item.value for item in ActionVerdict]},
                "reason": {"type": "string"},
                "evidence": {"type": "string"},
                "lesson": {"type": "string"},
            },
            "required": ["action_id", "action_type", "verdict", "reason", "evidence", "lesson"],
            "additionalProperties": False,
        }},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "format": "uuid"},
                "action_id": {"type": "string"},
                "category": {"type": "string", "enum": [item.value for item in FindingCategory]},
                "severity": {"type": "string", "enum": [item.value for item in FindingSeverity]},
                "claim": {"type": "string"},
                "issue": {"type": "string"},
                "evidence": {"type": "string"},
                "lesson": {"type": "string"},
            },
            "required": ["decision_id", "action_id", "category", "severity", "claim", "issue", "evidence", "lesson"],
            "additionalProperties": False,
        }},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["assessment", "summary", "action_reviews", "findings", "data_gaps"],
    "additionalProperties": False,
}
