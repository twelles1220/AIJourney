"""
Watchdog orchestrator — spit findings/tasks to the right specialist agent,
or recommend building a new agent when none exists.
"""

from __future__ import annotations

from typing import Any

from agents.base import DispatchRequest
from agents.registry import build_default_registry, registry


def ensure_registry():
    if not registry.list_specs():
        build_default_registry()
    return registry


def list_agents() -> list[dict]:
    return ensure_registry().list_specs()


def route_task(
    task: str,
    *,
    capability: str | None = None,
    finding_category: str | None = None,
    agent_id: str | None = None,
    payload: dict[str, Any] | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """
    Main Watchdog handoff API.

    execute=False → resolve only (matched agent or recommend_build)
    execute=True  → run the agent when matched
    """
    reg = ensure_registry()
    request = DispatchRequest(
        task=task,
        capability=capability,
        finding_category=finding_category,
        require_agent_id=agent_id,
        payload=payload or {},
    )
    if execute:
        return reg.dispatch(request)
    return reg.resolve(request)


def route_finding(finding: dict[str, Any], *, execute: bool = False, payload: dict | None = None) -> dict:
    """Route a Watchdog detector finding to the best specialist."""
    category = finding.get("category")
    task = finding.get("title") or finding.get("detail") or f"Handle {category} finding"
    return route_task(
        task,
        finding_category=category,
        payload={
            **(payload or {}),
            "finding": finding,
        },
        execute=execute,
    )


def route_findings_batch(findings: list[dict], *, execute: bool = False) -> dict[str, Any]:
    """Group findings by routed agent / build recommendations."""
    routed = []
    by_agent: dict[str, list] = {}
    recommendations: list[dict] = []

    for finding in findings:
        result = route_finding(finding, execute=execute)
        routed.append({"finding": finding.get("title"), "routing": result})
        if result.get("status") == "recommend_build":
            recommendations.append(result["recommendation"])
        elif result.get("agent"):
            aid = result["agent"]["id"]
            by_agent.setdefault(aid, []).append(finding.get("title"))

    return {
        "finding_count": len(findings),
        "by_agent": {k: {"count": len(v), "titles": v[:20]} for k, v in by_agent.items()},
        "recommend_build_count": len(recommendations),
        "recommendations": recommendations,
        "routed": routed,
    }
