"""
Shared contracts for specialist agents dispatched by Watchdog.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class AgentSpec:
    """Public catalog entry for an agent Watchdog can route to."""

    id: str
    name: str
    description: str
    capabilities: list[str]
    input_kinds: list[str]
    output_kinds: list[str]
    risks: list[str] = field(default_factory=list)
    status: str = "ready"  # ready | stub | recommended

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentResult:
    ok: bool
    agent_id: str
    summary: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    next_agents: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DispatchRequest:
    """Normalized handoff from Watchdog → specialist."""

    task: str
    capability: str | None = None
    finding_category: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    require_agent_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BuildRecommendation:
    """Emitted when no registered agent can handle a task."""

    proposed_id: str
    proposed_name: str
    trigger: str
    rationale: str
    suggested_capabilities: list[str]
    suggested_inputs: list[str]
    suggested_outputs: list[str]
    risks: list[str]
    priority: str = "high"

    def to_dict(self) -> dict:
        return asdict(self)


class Agent(Protocol):
    spec: AgentSpec

    def run(self, payload: dict[str, Any]) -> AgentResult: ...


Runner = Callable[[dict[str, Any]], AgentResult]
