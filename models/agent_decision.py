from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Decision(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class MonitorStatus(str, Enum):
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionActionType(str, Enum):
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class OpenProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: PositionSide
    reason: str = Field(min_length=1)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    valid_until: datetime

    @field_validator("valid_until")
    @classmethod
    def utc_valid_until(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("valid_until must be timezone-aware UTC")
        return value


class PositionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: UUID
    action: PositionActionType
    reason: str = Field(min_length=1)


class MonitorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    text: str = Field(min_length=1)
    status: MonitorStatus
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("expires_at must be timezone-aware UTC")
        return value


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Decision
    open_proposal: OpenProposal | None = None
    position_actions: list[PositionAction] = Field(default_factory=list)
    to_monitor: list[MonitorItem]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_monitor_ids(self):
        ids = [item.id for item in self.to_monitor]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate monitor id in one decision")
        action_ids = [item.position_id for item in self.position_actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("Duplicate action for one position")
        if self.decision is Decision.WAIT and self.open_proposal is not None:
            raise ValueError("WAIT cannot include an open proposal")
        if self.decision is Decision.BUY and (
            self.open_proposal is None or self.open_proposal.side is not PositionSide.LONG
        ):
            raise ValueError("BUY requires a LONG open proposal")
        if self.decision is Decision.SELL and (
            self.open_proposal is None or self.open_proposal.side is not PositionSide.SHORT
        ):
            raise ValueError("SELL requires a SHORT open proposal")
        return self


# The Responses API strict subset does not need Pydantic's date-time format,
# patterns or other validation keywords. Pydantic validates those after parsing.
AGENT_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [member.value for member in Decision]},
        "open_proposal": {
            "type": ["object", "null"],
            "properties": {
                "side": {"type": "string", "enum": [member.value for member in PositionSide]},
                "reason": {"type": "string"},
                "stop_loss": {"type": ["number", "null"]},
                "take_profit": {"type": ["number", "null"]},
                "valid_until": {"type": "string"},
            },
            "required": ["side", "reason", "stop_loss", "take_profit", "valid_until"],
            "additionalProperties": False,
        },
        "position_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "position_id": {"type": "string"},
                    "action": {"type": "string", "enum": [member.value for member in PositionActionType]},
                    "reason": {"type": "string"},
                },
                "required": ["position_id", "action", "reason"],
                "additionalProperties": False,
            },
        },
        "to_monitor": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "status": {"type": "string", "enum": [member.value for member in MonitorStatus]},
                    "expires_at": {"type": "string"},
                },
                "required": ["id", "text", "status", "expires_at"],
                "additionalProperties": False,
            },
        },
        "text": {"type": "string"},
    },
    "required": ["decision", "open_proposal", "position_actions", "to_monitor", "text"],
    "additionalProperties": False,
}
