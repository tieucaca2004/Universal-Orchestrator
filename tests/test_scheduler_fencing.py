import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import DuplicateError
from universal_orchestrator.execution.scheduler import ClaimStatus
from universal_orchestrator.execution.scheduler_manager import (
    LeaseLostError,
    LeaseNotHeldError,
    SchedulerLeaseManager,
    new_holder_id,
)


class LeaseManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "orchestrator.sqlite3"
        self.state = StateManager(self.db_path)
        self.leases = SchedulerLeaseManager(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _second_state(self) -> StateManager:
        """A fully independent StateManager/connection against the same
        file — the real cross-process shape, not just a second
        in-process object sharing the first one's mutex."""
        return StateManager(self.db_path)


class LeaseAcquireReleaseTests(LeaseManagerTestCase):
    def test_free_lease_can_be_acquired(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        self.assertEqual(epoch, 1)
        current = self.leases.current()
        self.assertEqual(current.holder_id, "h1")
        self.assertEqual(current.epoch, 1)

    def test_live_lease_cannot_be_acquired_by_another_holder(self):
        self.leases.acquire("h1", lease_seconds=30)
        self.assertIsNone(self.leases.try_acquire("h2", lease_seconds=30))
        with self.assertRaises(LeaseNotHeldError):
            self.leases.acquire("h2", lease_seconds=30)

    def test_epoch_increments_monotonically_across_acquisitions(self):
        e1 = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.release("h1", e1)
        e2 = self.leases.acquire("h2", lease_seconds=30)
        self.assertEqual(e2, e1 + 1)

    def test_release_requires_exact_current_holder_and_epoch(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        self.assertFalse(self.leases.release("h2", epoch))  # wrong holder
        self.assertFalse(self.leases.release("h1", epoch + 1))  # wrong epoch
        self.assertTrue(self.leases.release("h1", epoch))  # correct

    def test_renew_extends_expiry_without_changing_epoch(self):
        epoch = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.renew("h1", epoch, lease_seconds=30)
        time.sleep(0.1)
        # Still held by h1/epoch (renewed), so h2 cannot take over.
        self.assertIsNone(self.leases.try_acquire("h2", lease_seconds=30))
        self.assertEqual(self.leases.current().epoch, epoch)

    def test_renew_with_stale_epoch_raises_lease_lost(self):
        epoch = self.leases.acquire("h1", lease_seconds=0.05)
        time.sleep(0.1)
        self.leases.acquire("h2", lease_seconds=30)  # takeover after expiry
        with self.assertRaises(LeaseLostError):
            self.leases.renew("h1", epoch, lease_seconds=30)

    def test_expired_lease_can_be_taken_over_by_a_new_holder(self):
        e1 = self.leases.acquire("h1", lease_seconds=0.05)
        time.sleep(0.1)
        e2 = self.leases.acquire("h2", lease_seconds=30)
        self.assertEqual(e2, e1 + 1)
        self.assertEqual(self.leases.current().holder_id, "h2")

    def test_require_current_raises_once_superseded(self):
        epoch = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.require_current("h1", epoch)  # still fine
        time.sleep(0.1)
        self.leases.acquire("h2", lease_seconds=30)
        with self.assertRaises(LeaseLostError):
            self.leases.require_current("h1", epoch)


class ClaimTests(LeaseManagerTestCase):
    def test_claim_succeeds_under_the_current_epoch(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        self.assertTrue(self.leases.claim_task("task_1", "h1", epoch))
        claim = self.leases.get_claim("task_1")
        self.assertEqual(claim.status, ClaimStatus.CLAIMED)
        self.assertEqual(claim.epoch, epoch)

    def test_duplicate_claim_for_same_task_is_rejected(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        self.assertTrue(self.leases.claim_task("task_1", "h1", epoch))
        with self.assertRaises(DuplicateError):
            self.leases.claim_task("task_1", "h1", epoch)

    def test_claim_with_a_stale_epoch_fails_without_raising(self):
        e1 = self.leases.acquire("h1", lease_seconds=0.05)
        time.sleep(0.1)
        self.leases.acquire("h2", lease_seconds=30)
        self.assertFalse(self.leases.claim_task("task_1", "h1", e1))
        self.assertIsNone(self.leases.get_claim("task_1"))

    def test_complete_claim_succeeds_while_epoch_still_current(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        self.leases.claim_task("task_1", "h1", epoch)
        self.assertTrue(self.leases.complete_claim("task_1", "h1", epoch))
        self.assertEqual(self.leases.get_claim("task_1").status, ClaimStatus.COMPLETED)


class AtomicFencedWriteTests(LeaseManagerTestCase):
    """Requirement 2: prove an old fencing token cannot mutate protected
    state after a newer lease becomes authoritative — including across
    two genuinely independent StateManager connections on the same
    SQLite file, the real cross-process shape."""

    def test_stale_holder_cannot_complete_a_claim_after_takeover(self):
        epoch1 = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.claim_task("task_1", "h1", epoch1)
        time.sleep(0.1)

        # A second, independent connection — simulates a second process
        # taking over after h1's process stalled/died.
        other_state = self._second_state()
        other_leases = SchedulerLeaseManager(other_state)
        epoch2 = other_leases.acquire("h2", lease_seconds=30)
        self.assertEqual(epoch2, epoch1 + 1)

        # h1's own connection attempts the delayed, stale write.
        self.assertFalse(self.leases.complete_claim("task_1", "h1", epoch1))
        claim = self.leases.get_claim("task_1")
        self.assertEqual(claim.status, ClaimStatus.CLAIMED)  # never flipped to completed
        other_state.close()

    def test_new_authority_can_quarantine_the_stale_claim(self):
        epoch1 = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.claim_task("task_1", "h1", epoch1)
        time.sleep(0.1)

        other_state = self._second_state()
        other_leases = SchedulerLeaseManager(other_state)
        epoch2 = other_leases.acquire("h2", lease_seconds=30)

        self.assertTrue(other_leases.quarantine_claim("task_1", "h2", epoch2))
        self.assertEqual(self.leases.get_claim("task_1").status, ClaimStatus.QUARANTINED)
        other_state.close()

    def test_stale_holder_cannot_quarantine_on_someone_elses_behalf(self):
        epoch1 = self.leases.acquire("h1", lease_seconds=0.05)
        self.leases.claim_task("task_1", "h1", epoch1)
        time.sleep(0.1)

        other_state = self._second_state()
        other_leases = SchedulerLeaseManager(other_state)
        other_leases.acquire("h2", lease_seconds=30)

        # h1 is stale and is not the current authority — must fail.
        self.assertFalse(self.leases.quarantine_claim("task_1", "h1", epoch1))
        self.assertEqual(self.leases.get_claim("task_1").status, ClaimStatus.CLAIMED)
        other_state.close()

    def test_concurrent_acquire_attempts_yield_exactly_one_winner(self):
        results = []
        barrier = threading.Barrier(10)

        def attempt(i):
            barrier.wait()
            return self.leases.try_acquire(f"h{i}", lease_seconds=30)

        with ThreadPoolExecutor(max_workers=10) as pool:
            for epoch in pool.map(attempt, range(10)):
                results.append(epoch)

        winners = [e for e in results if e is not None]
        self.assertEqual(len(winners), 1)

    def test_concurrent_claim_attempts_for_the_same_task_yield_exactly_one_winner(self):
        epoch = self.leases.acquire("h1", lease_seconds=30)
        barrier = threading.Barrier(10)
        outcomes = []
        lock = threading.Lock()

        def attempt():
            barrier.wait()
            try:
                ok = self.leases.claim_task("task_race", "h1", epoch)
                with lock:
                    outcomes.append(ok)
            except DuplicateError:
                with lock:
                    outcomes.append(False)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count(True), 1)


if __name__ == "__main__":
    unittest.main()
