import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, cast, func, select, update
from sqlalchemy.orm import Session

from database.models.agent_decision import AgentDecisionORM
from database.models.agent_monitor_item import AgentMonitorItemORM
from models.agent_decision import AgentDecision, MonitorStatus
from models.market import Instrument


class MemoryConflictError(RuntimeError):
    """The market memory changed while this analysis was running."""


@dataclass(frozen=True)
class ActiveMonitor:
    id: str
    text: str
    expires_at: datetime
    updated_at: datetime
    created_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentMemoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def _expire(self, instrument: Instrument, now: datetime) -> None:
        self.session.execute(
            update(AgentMonitorItemORM)
            .where(
                AgentMonitorItemORM.instrument == instrument.value,
                AgentMonitorItemORM.status == MonitorStatus.ACTIVE.value,
                AgentMonitorItemORM.expires_at <= now,
            )
            .values(status="EXPIRED", updated_at=now)
        )

    def load_active(self, instrument: Instrument, now: datetime | None = None) -> list[ActiveMonitor]:
        now = now or _utc_now()
        self._expire(instrument, now)
        rows = self.session.scalars(
            select(AgentMonitorItemORM)
            .where(
                AgentMonitorItemORM.instrument == instrument.value,
                AgentMonitorItemORM.status == MonitorStatus.ACTIVE.value,
                AgentMonitorItemORM.expires_at > now,
            )
            .order_by(AgentMonitorItemORM.key)
        )
        return [
            ActiveMonitor(
                row.key, row.description,
                row.expires_at.astimezone(timezone.utc),
                row.updated_at.astimezone(timezone.utc),
                row.created_at.astimezone(timezone.utc),
            )
            for row in rows
        ]

    def exists(self, instrument: Instrument, candle_timestamp: datetime) -> bool:
        return self.session.scalar(
            select(AgentDecisionORM.id).where(
                AgentDecisionORM.instrument == instrument.value,
                AgentDecisionORM.candle_timestamp == candle_timestamp,
            )
        ) is not None

    def get_latest(self, instrument: Instrument, limit: int = 1) -> list[AgentDecisionORM]:
        return list(self.session.scalars(
            select(AgentDecisionORM)
            .where(AgentDecisionORM.instrument == instrument.value)
            .order_by(AgentDecisionORM.created_at.desc())
            .limit(limit)
        ))

    def save(
        self,
        instrument: Instrument,
        candle_timestamp: datetime,
        model: str,
        decision: AgentDecision,
        observed: list[ActiveMonitor],
        now: datetime | None = None,
        analysis_trace: dict | None = None,
    ) -> bool:
        """Persist one decision and all monitor changes in the caller's transaction."""
        now = now or _utc_now()
        # Serialize writers for this instrument. The comparison below rejects an
        # analysis based on an older memory snapshot after another writer commits.
        self.session.scalar(select(func.pg_advisory_xact_lock(
            cast(func.hashtext(f"agent-memory:{instrument.value}"), BigInteger)
        )))
        if self.exists(instrument, candle_timestamp):
            return False
        current = self.load_active(instrument, now)
        if current != observed:
            raise MemoryConflictError("Active observations changed during analysis; retry with fresh memory")

        max_items = int(os.getenv("AGENT_MAX_MONITOR_ITEMS", "20"))
        max_ttl = timedelta(hours=int(os.getenv("AGENT_MAX_MONITOR_HOURS", "24")))
        if max_items < 1 or max_ttl <= timedelta(0):
            raise ValueError("Agent monitor limits must be positive")
        if len(decision.to_monitor) > max_items:
            raise ValueError(f"At most {max_items} observations may be returned")

        keys = [item.id for item in decision.to_monitor]
        existing = {
            row.key: row
            for row in self.session.scalars(
                select(AgentMonitorItemORM).where(
                    AgentMonitorItemORM.instrument == instrument.value,
                    AgentMonitorItemORM.key.in_(keys),
                )
            )
        } if keys else {}

        active_after = len(current)
        for item in decision.to_monitor:
            row = existing.get(item.id)
            if row is not None and row.status != MonitorStatus.ACTIVE.value:
                raise ValueError(f"Observation {item.id} is closed; use a new id")
            if row is None and item.status != MonitorStatus.ACTIVE:
                raise ValueError(f"Cannot close unknown observation {item.id}")
            if item.status == MonitorStatus.ACTIVE:
                if item.expires_at <= now:
                    raise ValueError(f"Observation {item.id} expires_at must be in the future")
                start = row.created_at if row is not None else now
                if item.expires_at > start + max_ttl:
                    raise ValueError(f"Observation {item.id} exceeds maximum lifetime")
                if row is None:
                    active_after += 1
            else:
                active_after -= 1
        if active_after > max_items:
            raise ValueError(f"At most {max_items} active observations are allowed")

        decision_id = uuid4()
        self.session.add(AgentDecisionORM(
            id=decision_id,
            instrument=instrument.value,
            candle_timestamp=candle_timestamp,
            created_at=now,
            decision=decision.decision.value,
            text=decision.text,
            model=model,
            open_proposal=decision.open_proposal.model_dump(mode="json") if decision.open_proposal else None,
            position_actions=[item.model_dump(mode="json") for item in decision.position_actions],
            pending_order_actions=[item.model_dump(mode="json") for item in decision.pending_order_actions],
            analysis_trace=analysis_trace,
            paper_status="PENDING",
        ))
        for item in decision.to_monitor:
            row = existing.get(item.id)
            if row is None:
                self.session.add(AgentMonitorItemORM(
                    id=uuid4(),
                    instrument=instrument.value,
                    key=item.id,
                    description=item.text,
                    status=item.status.value,
                    created_at=now,
                    updated_at=now,
                    expires_at=item.expires_at,
                    created_by_decision_id=decision_id,
                ))
            else:
                row.description = item.text
                row.status = item.status.value
                row.expires_at = item.expires_at
                row.updated_at = now
        self.session.flush()
        return True
