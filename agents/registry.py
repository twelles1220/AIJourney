"""
Agent registry — Watchdog looks up specialists by capability / finding category.

If nothing matches, emit a BuildRecommendation instead of inventing behavior.
"""

from __future__ import annotations

import re
from typing import Any

from agents.base import Agent, AgentResult, AgentSpec, BuildRecommendation, DispatchRequest


# Finding categories from Watchdog detectors → preferred capability tags
FINDING_TO_CAPABILITY: dict[str, list[str]] = {
    "duplicate": ["fuzzy_match", "rpa_merge"],
    "incomplete": ["schema_normalize"],
    "unknown_value": ["schema_normalize"],
    "integrity": ["schema_normalize"],
    "bloat": ["fuzzy_match"],
    "security": ["executive_report"],  # surface to stakeholders; remediation stays in guardian
}

# Free-text task keywords → capability
TASK_KEYWORDS: list[tuple[str, str]] = [
    (r"schema|normalize|csv.?map|column.?header|bloomerang.?export|mash(ed)? names?", "schema_normalize"),
    (r"fuzzy|typo|similar(ity)?|jon smith|near.?duplicate|rapidfuzz|fuzzywuzzy", "fuzzy_match"),
    (r"rpa|playwright|selenium|ui.?merge|sam\b|headless|click through", "rpa_merge"),
    (r"synthetic|dummy.?data|fake.?data|test.?corpus|privacy.?safe", "synthetic_data"),
    (r"executive|roi|client.?report|stakeholder|presentation|email.?summary", "executive_report"),
]


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self._agents[agent.spec.id] = agent

    def list_specs(self) -> list[dict]:
        return [a.spec.to_dict() for a in self._agents.values()]

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def find_by_capability(self, capability: str) -> list[Agent]:
        return [a for a in self._agents.values() if capability in a.spec.capabilities]

    def infer_capability(self, request: DispatchRequest) -> str | None:
        if request.capability:
            return request.capability
        if request.require_agent_id:
            agent = self.get(request.require_agent_id)
            return agent.spec.capabilities[0] if agent and agent.spec.capabilities else None
        if request.finding_category:
            caps = FINDING_TO_CAPABILITY.get(request.finding_category, [])
            if caps:
                return caps[0]
        task = (request.task or "").lower()
        for pattern, cap in TASK_KEYWORDS:
            if re.search(pattern, task, re.I):
                return cap
        return None

    def resolve(self, request: DispatchRequest) -> dict[str, Any]:
        """
        Resolve a dispatch request to either an agent handoff plan or a build recommendation.
        Does not execute the agent — call dispatch() for that.
        """
        if request.require_agent_id:
            agent = self.get(request.require_agent_id)
            if not agent:
                return {
                    "status": "recommend_build",
                    "recommendation": _recommend_for_missing_id(
                        request.require_agent_id, request
                    ).to_dict(),
                }
            return {
                "status": "matched",
                "agent": agent.spec.to_dict(),
                "capability": request.capability or (
                    agent.spec.capabilities[0] if agent.spec.capabilities else None
                ),
            }

        capability = self.infer_capability(request)
        if not capability:
            return {
                "status": "recommend_build",
                "recommendation": _recommend_unknown_task(request).to_dict(),
            }

        matches = self.find_by_capability(capability)
        if not matches:
            return {
                "status": "recommend_build",
                "recommendation": _recommend_for_capability(capability, request).to_dict(),
            }

        agent = matches[0]
        return {
            "status": "matched",
            "agent": agent.spec.to_dict(),
            "capability": capability,
            "alternates": [a.spec.id for a in matches[1:]],
        }

    def dispatch(self, request: DispatchRequest) -> dict[str, Any]:
        """Resolve + run the matched agent, or return a build recommendation."""
        resolution = self.resolve(request)
        if resolution["status"] != "matched":
            return resolution

        agent = self.get(resolution["agent"]["id"])
        assert agent is not None
        try:
            result: AgentResult = agent.run(request.payload or {"task": request.task})
        except Exception as exc:  # noqa: BLE001 — surface to orchestrator
            result = AgentResult(
                ok=False,
                agent_id=agent.spec.id,
                summary=f"Agent crashed: {exc}",
                errors=[str(exc)],
            )
        return {
            "status": "completed" if result.ok else "failed",
            "agent": agent.spec.to_dict(),
            "capability": resolution.get("capability"),
            "result": result.to_dict(),
        }


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:48] or "custom_agent"


def _recommend_unknown_task(request: DispatchRequest) -> BuildRecommendation:
    slug = _slug(request.task or "unspecified_task")
    return BuildRecommendation(
        proposed_id=f"agent_{slug}",
        proposed_name=f"Custom Agent for: {(request.task or 'unspecified')[:80]}",
        trigger=request.task or "unspecified task",
        rationale=(
            "Watchdog could not map this task to a registered capability "
            "(schema_normalize, fuzzy_match, rpa_merge, synthetic_data, executive_report)."
        ),
        suggested_capabilities=[slug],
        suggested_inputs=["task_payload", "context_from_watchdog"],
        suggested_outputs=["remediation_artifact", "audit_summary"],
        risks=["Unknown blast radius until scoped", "May need human-in-the-loop"],
        priority="high",
    )


def _recommend_for_capability(capability: str, request: DispatchRequest) -> BuildRecommendation:
    return BuildRecommendation(
        proposed_id=f"agent_{capability}",
        proposed_name=f"{capability.replace('_', ' ').title()} Agent",
        trigger=request.task or capability,
        rationale=(
            f"Capability '{capability}' is required for this Watchdog handoff, "
            "but no agent currently implements it."
        ),
        suggested_capabilities=[capability],
        suggested_inputs=["watchdog_finding", "source_artifact"],
        suggested_outputs=["remediation_artifact"],
        risks=["Capability gap blocks automated remediation"],
        priority="critical",
    )


def _recommend_for_missing_id(agent_id: str, request: DispatchRequest) -> BuildRecommendation:
    return BuildRecommendation(
        proposed_id=agent_id,
        proposed_name=agent_id.replace("_", " ").title(),
        trigger=request.task or f"explicit request for {agent_id}",
        rationale=f"No agent registered with id '{agent_id}'.",
        suggested_capabilities=[agent_id],
        suggested_inputs=["payload"],
        suggested_outputs=["result"],
        risks=["Broken handoff from Watchdog"],
        priority="critical",
    )


# Singleton used by Watchdog + CLI
registry = AgentRegistry()


def build_default_registry() -> AgentRegistry:
    """Register all built-in specialists. Safe to call multiple times."""
    from agents.executive_reporting import ExecutiveReportingAgent
    from agents.fuzzy_match import FuzzyMatchAgent
    from agents.rpa_bypass import RPABypassAgent
    from agents.schema_mapper import SchemaMapperAgent
    from agents.synthetic_data import SyntheticDataAgent

    for agent in (
        SchemaMapperAgent(),
        FuzzyMatchAgent(),
        SyntheticDataAgent(),
        RPABypassAgent(),
        ExecutiveReportingAgent(),
    ):
        registry.register(agent)
    return registry
