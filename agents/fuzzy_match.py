"""
Fuzzy-Match Decision Engine

Scores near-duplicate donor pairs with RapidFuzz (difflib fallback).
Auto-merge >= auto_threshold (default 98); review band otherwise.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from agents.base import AgentResult, AgentSpec

try:
    from rapidfuzz import fuzz

    def _ratio(a: str, b: str) -> float:
        return float(fuzz.token_sort_ratio(a, b))

except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0


def _identity_key(row: dict) -> str:
    parts = [
        (row.get("first_name") or ""),
        (row.get("last_name") or ""),
        (row.get("email") or ""),
        (row.get("phone") or ""),
        (row.get("address_line1") or ""),
        (row.get("postal_code") or ""),
    ]
    return " | ".join(p.strip().lower() for p in parts)


def _load_rows(payload: dict[str, Any]) -> list[dict]:
    if payload.get("records"):
        return list(payload["records"])
    csv_path = payload.get("csv_path")
    if csv_path:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    raise ValueError("Provide records or csv_path")


def score_pairs(
    rows: list[dict],
    *,
    auto_threshold: float = 98.0,
    review_threshold: float = 80.0,
) -> dict[str, Any]:
    auto_merge: list[dict] = []
    review: list[dict] = []
    n = len(rows)

    for i in range(n):
        for j in range(i + 1, n):
            left, right = rows[i], rows[j]
            # Exact email shortcut
            le = (left.get("email") or "").strip().lower()
            re_ = (right.get("email") or "").strip().lower()
            if le and re_ and le == re_:
                score = 100.0
                reason = "exact_email"
            else:
                score = _ratio(_identity_key(left), _identity_key(right))
                reason = "fuzzy_identity"

            if score < review_threshold:
                continue

            pair = {
                "left_index": i,
                "right_index": j,
                "left": left,
                "right": right,
                "score": round(score, 2),
                "reason": reason,
                "decision": (
                    "auto_merge" if score >= auto_threshold else "review_needed"
                ),
            }
            if pair["decision"] == "auto_merge":
                auto_merge.append(pair)
            else:
                review.append(pair)

    auto_merge.sort(key=lambda p: -p["score"])
    review.sort(key=lambda p: -p["score"])
    return {
        "auto_threshold": auto_threshold,
        "review_threshold": review_threshold,
        "pair_count_scanned": n * (n - 1) // 2,
        "auto_merge_count": len(auto_merge),
        "review_count": len(review),
        "auto_merge": auto_merge,
        "review_needed": review,
    }


class FuzzyMatchAgent:
    spec = AgentSpec(
        id="fuzzy_match",
        name="Fuzzy-Match Decision Engine",
        description=(
            "Scores near-duplicate records with RapidFuzz. Auto-merges >=98% "
            "confidence and emits a Review Needed queue for 80–97%."
        ),
        capabilities=["fuzzy_match"],
        input_kinds=["normalized_csv", "donor_records"],
        output_kinds=["auto_merge_queue", "review_needed_csv"],
        risks=[
            "False positives near threshold — keep review band human-gated",
            "Do not execute CRM merges here; hand off to rpa_bypass",
        ],
    )

    def run(self, payload: dict[str, Any]) -> AgentResult:
        try:
            rows = _load_rows(payload)
        except ValueError as exc:
            return AgentResult(
                ok=False,
                agent_id=self.spec.id,
                summary=str(exc),
                errors=[str(exc)],
            )

        auto_threshold = float(payload.get("auto_threshold", 98))
        review_threshold = float(payload.get("review_threshold", 80))
        result = score_pairs(
            rows, auto_threshold=auto_threshold, review_threshold=review_threshold
        )

        output_dir = Path(payload.get("output_dir") or "outputs/fuzzy_match")
        output_dir.mkdir(parents=True, exist_ok=True)

        auto_path = output_dir / "auto_merge_queue.json"
        review_path = output_dir / "review_needed.csv"
        summary_path = output_dir / "fuzzy_summary.json"

        auto_path.write_text(json.dumps(result["auto_merge"], indent=2), encoding="utf-8")
        summary_path.write_text(
            json.dumps(
                {
                    k: result[k]
                    for k in (
                        "auto_threshold",
                        "review_threshold",
                        "pair_count_scanned",
                        "auto_merge_count",
                        "review_count",
                    )
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with review_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "score",
                    "reason",
                    "left_email",
                    "right_email",
                    "left_name",
                    "right_name",
                    "left_index",
                    "right_index",
                ],
            )
            writer.writeheader()
            for p in result["review_needed"]:
                writer.writerow(
                    {
                        "score": p["score"],
                        "reason": p["reason"],
                        "left_email": (p["left"].get("email") or ""),
                        "right_email": (p["right"].get("email") or ""),
                        "left_name": f"{p['left'].get('first_name', '')} {p['left'].get('last_name', '')}".strip(),
                        "right_name": f"{p['right'].get('first_name', '')} {p['right'].get('last_name', '')}".strip(),
                        "left_index": p["left_index"],
                        "right_index": p["right_index"],
                    }
                )

        next_agents = []
        if result["auto_merge_count"]:
            next_agents.append("rpa_bypass")
        next_agents.append("executive_report")

        return AgentResult(
            ok=True,
            agent_id=self.spec.id,
            summary=(
                f"Scanned {result['pair_count_scanned']} pairs → "
                f"{result['auto_merge_count']} auto-merge, "
                f"{result['review_count']} review-needed."
            ),
            artifacts={
                "auto_merge_queue": str(auto_path),
                "review_needed_csv": str(review_path),
                "summary_json": str(summary_path),
                "auto_merge_count": result["auto_merge_count"],
                "review_count": result["review_count"],
                "auto_merge": result["auto_merge"],
                "review_needed": result["review_needed"],
            },
            next_agents=next_agents,
        )
