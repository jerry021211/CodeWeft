"""Legacy Team contracts, retained for existing TeamRun/Attempt/Candidate workflows."""

from codeagent.legacy.teams.models import (
    AgentSessionCheckpointRecord,
    AgentSessionRecord,
    AgentSessionState,
    AttemptPlanRecord,
    AttemptPlanStatus,
    CandidateRecord,
    CandidateStatus,
    DependencyRequirement,
    ResourceLeaseRecord,
    TaskAttemptRecord,
    TaskAttemptState,
    TaskSchedulingRecord,
    TeamAgentRecord,
    TeamAgentRole,
    TeamMessageRecord,
    TeamPlanRevisionRecord,
    TeamPlanStatus,
    TeamRunRecord,
    TeamRunState,
    ToolExecutionRecord,
    ValidationRunRecord,
    WorktreeBindingRecord,
)
# Load services on demand: model imports must not initialize the Worktree runtime.
_SERVICE_MODULES = {
    "MessageBus": "bus", "CandidateService": "candidates",
    "ManualIntegrationVerifier": "integration", "LeadTeamPlanTool": "lead",
    "AgentSessionRunner": "session", "TeamStatusTool": "tools",
    "create_lead_tools": "tools", "create_teammate_tools": "tools",
    "ActiveTeamRootToolExecutionGate": "tool_gate", "ReadOnlyTeamToolExecutionGate": "tool_gate",
    "TeamPlannerToolExecutionGate": "tool_gate", "TeamToolExecutionGate": "tool_gate",
}


def __getattr__(name):
    if name not in _SERVICE_MODULES:
        raise AttributeError(name)
    from importlib import import_module
    value = getattr(import_module(f"codeagent.legacy.teams.{_SERVICE_MODULES[name]}"), name)
    globals()[name] = value
    return value


__all__ = [
    "AgentSessionCheckpointRecord",
    "AgentSessionRecord",
    "AgentSessionRunner",
    "AgentSessionState",
    "ActiveTeamRootToolExecutionGate",
    "AttemptPlanRecord",
    "AttemptPlanStatus",
    "CandidateRecord",
    "CandidateStatus",
    "CandidateService",
    "DependencyRequirement",
    "MessageBus",
    "ManualIntegrationVerifier",
    "LeadTeamPlanTool",
    "ResourceLeaseRecord",
    "ReadOnlyTeamToolExecutionGate",
    "TaskAttemptRecord",
    "TaskAttemptState",
    "TaskSchedulingRecord",
    "TeamAgentRecord",
    "TeamAgentRole",
    "TeamMessageRecord",
    "TeamPlanRevisionRecord",
    "TeamPlanStatus",
    "TeamPlannerToolExecutionGate",
    "TeamRunRecord",
    "TeamRunState",
    "TeamStatusTool",
    "TeamToolExecutionGate",
    "ToolExecutionRecord",
    "ValidationRunRecord",
    "WorktreeBindingRecord",
    "create_teammate_tools",
    "create_lead_tools",
]
