from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from models.market import Timeframe


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


class TriggerCondition(str, Enum):
    CLOSE_ABOVE = "CLOSE_ABOVE"
    CLOSE_BELOW = "CLOSE_BELOW"


class PendingActionType(str, Enum):
    CREATE = "CREATE"
    AMEND = "AMEND"
    CANCEL = "CANCEL"
    HOLD = "HOLD"


class PendingOrderType(str, Enum):
    BUY_LIMIT = "BUY_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_LIMIT = "SELL_LIMIT"
    SELL_STOP = "SELL_STOP"


class PendingInvalidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeframe: Timeframe
    operator: TriggerCondition
    level: Decimal = Field(gt=0)


class PendingOrderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: PendingActionType
    order_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    order_type: PendingOrderType | None = None
    entry_level_usd: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    scenario_key: str | None = Field(default=None, max_length=80)
    setup_type: str | None = Field(default=None, max_length=80)
    invalidating_condition: PendingInvalidation | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.expires_at is not None and (self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0)):
            raise ValueError("expires_at must be UTC")
        if self.action is PendingActionType.CREATE:
            if self.order_id is not None or self.expected_revision is not None:
                raise ValueError("CREATE cannot target an existing order")
            if any(getattr(self, key) is None for key in (
                "order_type", "entry_level_usd", "stop_loss", "take_profit", "expires_at", "scenario_key", "setup_type")):
                raise ValueError("CREATE requires complete order levels, TTL, scenario and setup")
        else:
            if self.order_id is None or self.expected_revision is None:
                raise ValueError("Existing order action requires id and revision")
            if self.action in (PendingActionType.CANCEL, PendingActionType.HOLD) and any(
                getattr(self, key) is not None for key in (
                    "order_type", "entry_level_usd", "stop_loss", "take_profit", "expires_at", "scenario_key", "setup_type", "invalidating_condition")):
                raise ValueError("CANCEL/HOLD cannot change order fields")
        return self


class OpenProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: PositionSide
    reason: str = Field(min_length=1)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    valid_until: datetime
    setup_type: str | None = Field(default=None, max_length=80)
    market_regime: str | None = Field(default=None, max_length=80)
    signal_timeframe: Timeframe | None = None
    trigger_candle_timestamp: datetime | None = None
    trigger_level: Decimal | None = Field(default=None, gt=0)
    trigger_condition: TriggerCondition | None = None
    invalidation_level: Decimal | None = Field(default=None, gt=0)
    expected_rr_after_costs: Decimal | None = Field(default=None, ge=0)
    signal_age_seconds: int | None = Field(default=None, ge=0)
    countertrend: bool | None = None

    @field_validator("valid_until", "trigger_candle_timestamp")
    @classmethod
    def utc_valid_until(cls, value: datetime) -> datetime:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("Timestamp must be timezone-aware UTC")
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
    pending_order_actions: list[PendingOrderAction] = Field(default_factory=list, max_length=6)
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
        pending_ids = [item.order_id for item in self.pending_order_actions if item.order_id]
        if len(pending_ids) != len(set(pending_ids)):
            raise ValueError("Duplicate action for one pending order")
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
                "setup_type": {"type": ["string", "null"]},
                "market_regime": {"type": ["string", "null"]},
                "signal_timeframe": {"type": ["string", "null"], "enum": [item.value for item in Timeframe] + [None]},
                "trigger_candle_timestamp": {"type": ["string", "null"]},
                "trigger_level": {"type": ["number", "null"]},
                "trigger_condition": {"type": ["string", "null"], "enum": [item.value for item in TriggerCondition] + [None]},
                "invalidation_level": {"type": ["number", "null"]},
                "expected_rr_after_costs": {"type": ["number", "null"]},
                "signal_age_seconds": {"type": ["integer", "null"]},
                "countertrend": {"type": ["boolean", "null"]},
            },
            "required": ["side", "reason", "stop_loss", "take_profit", "valid_until",
                         "setup_type", "market_regime", "signal_timeframe", "trigger_candle_timestamp",
                         "trigger_level", "trigger_condition", "invalidation_level",
                         "expected_rr_after_costs", "signal_age_seconds", "countertrend"],
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
        "pending_order_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [member.value for member in PendingActionType]},
                    "order_id": {"type": ["string", "null"]},
                    "expected_revision": {"type": ["integer", "null"]},
                    "order_type": {"type": ["string", "null"], "enum": [member.value for member in PendingOrderType] + [None]},
                    "entry_level_usd": {"type": ["number", "null"]},
                    "stop_loss": {"type": ["number", "null"]},
                    "take_profit": {"type": ["number", "null"]},
                    "expires_at": {"type": ["string", "null"]},
                    "scenario_key": {"type": ["string", "null"]},
                    "setup_type": {"type": ["string", "null"]},
                    "invalidating_condition": {"type": ["object", "null"],
                        "properties": {"timeframe": {"type": "string", "enum": [item.value for item in Timeframe]},
                                       "operator": {"type": "string", "enum": [item.value for item in TriggerCondition]},
                                       "level": {"type": "number"}},
                        "required": ["timeframe", "operator", "level"], "additionalProperties": False},
                    "reason": {"type": "string"},
                },
                "required": ["action", "order_id", "expected_revision", "order_type", "entry_level_usd",
                             "stop_loss", "take_profit", "expires_at", "scenario_key", "setup_type",
                             "invalidating_condition", "reason"],
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
    "required": ["decision", "open_proposal", "position_actions", "pending_order_actions", "to_monitor", "text"],
    "additionalProperties": False,
}
