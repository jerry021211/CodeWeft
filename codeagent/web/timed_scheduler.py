"""Clock-driven admission into the existing permission-aware run queue."""

import logging
import threading
from datetime import UTC, datetime

from codeagent.web.workspaces import WorkspaceCatalog

logger = logging.getLogger(__name__)


class TimedScheduler:
    def __init__(self, repository, run_scheduler, workspace_catalog: WorkspaceCatalog, *, poll_seconds=1.0):
        self.repository = repository
        self.run_scheduler = run_scheduler
        self.workspace_catalog = workspace_catalog
        self.poll_seconds = poll_seconds
        self._stopping = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self.repository.recover_schedule_dispatches()
            self._stopping.clear()
            self._thread = threading.Thread(target=self._worker, name="codeagent-timer", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stopping.set()
            if self._thread is not None:
                self._thread.join()
                self._thread = None

    def _worker(self):
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Timed task polling failed")
            self._stopping.wait(self.poll_seconds)

    def tick(self, now=None):
        now = now or datetime.now(UTC)
        # Bounded batches keep shutdown responsive even after a long outage.
        for _ in range(100):
            if self._stopping.is_set():
                break
            claim = self.repository.claim_due_schedule(now=now)
            if claim is None:
                break
            item, occurrence_id, skipped = claim
            if skipped:
                continue
            try:
                workspace = self.workspace_catalog.resolve(item["workspace"])
                conversation = self.repository.create_conversation(
                    title=f"定时 · {item['name']}"[:200], workspace=str(workspace),
                )
                self.repository.update_schedule_occurrence(
                    occurrence_id, status="dispatching", conversation_id=conversation.id,
                )
                run = self.run_scheduler.submit(
                    conversation.id, item["prompt"], read_only=item["read_only"],
                )
                self.repository.update_schedule_occurrence(occurrence_id, status="submitted", run_id=run.id)
            except Exception as exc:
                logger.exception("Timed task dispatch failed: %s", item["id"])
                self.repository.update_schedule_occurrence(
                    occurrence_id, status="failed", error=str(exc)[:1000],
                )
