import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.connection import engine
from database.models.agent_decision import AgentDecisionORM
from database.models.agent_monitor_item import AgentMonitorItemORM
from models.agent_decision import AgentDecision
from models.market import Instrument
from repositories.agent_memory_repository import AgentMemoryRepository, MemoryConflictError


def decision(items, direction="WAIT"):
    return AgentDecision.model_validate({
        "decision": direction,
        "to_monitor": items,
        "text": "Wynik testowy",
    })


class AgentMemoryPostgresTests(unittest.TestCase):
    def test_lifecycle_unique_keys_expiry_and_atomicity(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        instrument = Instrument.BTC_USD
        # The entire test runs inside an outer transaction, rolled back even on failure.
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, autoflush=False) as session:
                    repo = AgentMemoryRepository(session)
                    initial = repo.load_active(instrument, now)
                    key = f"test_memory_{now.timestamp():.0f}"
                    first_candle = now - timedelta(days=100)
                    expiry = now + timedelta(hours=1)
                    item = {"id": key, "text": "Obserwuj wybicie", "status": "ACTIVE", "expires_at": expiry}
                    trace = {"tool_calls": [{"name": "get_price", "status": "OK"}]}
                    self.assertTrue(repo.save(instrument, first_candle, "test-model", decision([item]), initial, now,
                                              analysis_trace=trace))
                    self.assertEqual(session.scalar(select(AgentDecisionORM.analysis_trace).where(
                        AgentDecisionORM.instrument == instrument.value,
                        AgentDecisionORM.candle_timestamp == first_candle,
                    )), trace)
                    self.assertFalse(repo.save(instrument, first_candle, "test-model", decision([]), initial, now))
                    active = repo.load_active(instrument, now)
                    self.assertEqual(len([row for row in active if row.id == key]), 1)

                    # Omission leaves the row active; updating by key keeps one row.
                    repo.save(instrument, first_candle + timedelta(minutes=15), "test-model", decision([]), active, now + timedelta(minutes=1))
                    active = repo.load_active(instrument, now + timedelta(minutes=1))
                    self.assertTrue(any(row.id == key for row in active))
                    changed = {**item, "text": "Zaktualizowany warunek", "expires_at": expiry + timedelta(minutes=15)}
                    repo.save(instrument, first_candle + timedelta(minutes=30), "test-model", decision([changed]), active, now + timedelta(minutes=2))
                    row = session.scalar(select(AgentMonitorItemORM).where(AgentMonitorItemORM.key == key))
                    self.assertEqual(row.description, "Zaktualizowany warunek")
                    self.assertEqual(len(session.scalars(select(AgentMonitorItemORM).where(AgentMonitorItemORM.key == key)).all()), 1)

                    # A database constraint failure rolls back all writes in the savepoint.
                    with self.assertRaises(IntegrityError):
                        with session.begin_nested():
                            session.add(AgentMonitorItemORM(
                                id=uuid4(), instrument=instrument.value, key=key,
                                description="Duplikat", status="ACTIVE",
                                created_at=now, updated_at=now, expires_at=expiry,
                                created_by_decision_id=session.scalar(
                                    select(AgentDecisionORM.id).where(
                                        AgentDecisionORM.instrument == instrument.value,
                                        AgentDecisionORM.candle_timestamp == first_candle,
                                    )
                                ),
                            ))
                            session.flush()
                    self.assertEqual(len(session.scalars(select(AgentMonitorItemORM).where(AgentMonitorItemORM.key == key)).all()), 1)

                    with self.assertRaises(MemoryConflictError):
                        repo.save(instrument, first_candle + timedelta(minutes=45), "test-model", decision([]), active, now + timedelta(minutes=3))
                    active = repo.load_active(instrument, now + timedelta(minutes=3))
                    with self.assertRaises(ValueError):
                        too_long = {**changed, "expires_at": now + timedelta(hours=25)}
                        repo.save(instrument, first_candle + timedelta(minutes=45), "test-model", decision([too_long]), active, now + timedelta(minutes=3))
                    with self.assertRaises(ValueError):
                        expired_request = {**changed, "expires_at": now - timedelta(minutes=1)}
                        repo.save(instrument, first_candle + timedelta(minutes=45), "test-model", decision([expired_request]), active, now + timedelta(minutes=3))
                    self.assertFalse(repo.exists(instrument, first_candle + timedelta(minutes=45)))

                    closed = {**changed, "status": "TRIGGERED"}
                    repo.save(instrument, first_candle + timedelta(minutes=45), "test-model", decision([closed]), active, now + timedelta(minutes=3))
                    self.assertFalse(any(row.id == key for row in repo.load_active(instrument, now + timedelta(minutes=4))))
                    self.assertEqual(row.status, "TRIGGERED")
                    with self.assertRaises(ValueError):
                        repo.save(instrument, first_candle + timedelta(minutes=60), "test-model", decision([item]), repo.load_active(instrument, now + timedelta(minutes=4)), now + timedelta(minutes=4))
                    self.assertFalse(repo.exists(instrument, first_candle + timedelta(minutes=60)))

                    expiring_key = key + "_expiry"
                    new_item = {**item, "id": expiring_key, "expires_at": now + timedelta(minutes=7)}
                    repo.save(instrument, first_candle + timedelta(minutes=60), "test-model", decision([new_item]), repo.load_active(instrument, now + timedelta(minutes=4)), now + timedelta(minutes=4))
                    self.assertFalse(any(row.id == expiring_key for row in repo.load_active(instrument, now + timedelta(minutes=8))))
                    expired = session.scalar(select(AgentMonitorItemORM).where(AgentMonitorItemORM.key == expiring_key))
                    self.assertEqual(expired.status, "EXPIRED")
                    self.assertEqual(session.scalar(select(AgentDecisionORM.decision).where(
                        AgentDecisionORM.instrument == instrument.value,
                        AgentDecisionORM.candle_timestamp == first_candle,
                    )), "WAIT")
            finally:
                transaction.rollback()


if __name__ == "__main__":
    unittest.main()
