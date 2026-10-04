"""Explicit per-trial call quotas, independent of production execution limits."""
from codeagent.runtime.execution import ExecutionStopped, RunBudget


class EvaluationBudget(RunBudget):
    def __init__(self, config, *, model_calls, tool_calls):
        super().__init__(config)
        self.call_limits = {"model": model_calls, "tool": tool_calls}

    def reserve(self, kind):
        with self._lock:
            self.check()
            if getattr(self.state, f"{kind}_calls") >= self.call_limits[kind]:
                self.state.stop_reason = f"budget_exceeded:evaluation_{kind}_calls"
                raise ExecutionStopped(self.state.stop_reason)
            super().reserve(kind)
