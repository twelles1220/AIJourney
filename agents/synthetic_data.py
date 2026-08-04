"""
Synthetic Data Generator

Inspects a client's schema (or a sample CSV) and emits privacy-safe dummy rows
that mimic formatting quirks, missing fields, and duplicates — for safe testing
before live CRM remediations.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from pathlib import Path
from typing import Any

from agents.base import AgentResult, AgentSpec

FIRST = [
    "Alex", "Jordan", "Sam", "Riley", "Casey", "Morgan", "Quinn", "Avery",
    "Jamie", "Taylor", "Jon", "John", "Chris", "Kris", "Pat", "Patricia",
]
LAST = [
    "Smith", "Smyth", "Lee", "Nguyen", "Patel", "Brooks", "Garcia", "Brown",
    "Johnson", "Williams", "Jones", "Miller", "Davis", "Wilson", "Moore",
]
STREETS = ["Main St", "Main Street", "Oak Ave", "Pine Rd", "Cedar Ct", "River Rd"]
CITIES = ["Austin", "Denver", "Seattle", "Chicago", "Boston", "Miami"]
STATES = ["TX", "CO", "WA", "IL", "MA", "FL"]
STATUSES = ["active", "lapsed", "prospect", "unknown", "N/A", ""]
PLACEHOLDERS = ["N/A", "Unknown", "TBD", "", "test@test.com"]


def _rng(seed: str | int | None) -> random.Random:
    if seed is None:
        return random.Random(42)
    if isinstance(seed, int):
        return random.Random(seed)
    digest = hashlib.sha256(str(seed).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def infer_schema_from_csv(csv_path: str) -> list[str]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or [])


def generate_rows(
    columns: list[str],
    n: int,
    *,
    seed: str | int | None = 42,
    quirk_rate: float = 0.25,
) -> list[dict]:
    rng = _rng(seed)
    rows: list[dict] = []

    # Pre-create some intentional near-duplicates
    twin_budget = max(1, n // 50)

    for i in range(n):
        first = rng.choice(FIRST)
        last = rng.choice(LAST)
        email = f"{first}.{last}{i}@example.com".lower()
        phone = f"555-{rng.randint(1000, 9999)}"
        street = rng.choice(STREETS)
        city = rng.choice(CITIES)
        state = rng.choice(STATES)
        zip_code = f"{rng.randint(10000, 99999)}"
        status = rng.choice(STATUSES)
        notes = ""

        row_as_master = {
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": phone,
            "address_line1": f"{rng.randint(1, 999)} {street}",
            "city": city,
            "state": state,
            "postal_code": zip_code,
            "status": status,
            "notes": notes,
            "full_name": f"{first} {last}",
            "address": f"{rng.randint(1, 999)} {street}, {city} {zip_code}",
        }

        # Inject quirks
        if rng.random() < quirk_rate:
            quirk = rng.choice(
                ["missing_email", "placeholder", "mashed_zip", "typo_name", "test_email"]
            )
            if quirk == "missing_email":
                row_as_master["email"] = ""
            elif quirk == "placeholder":
                row_as_master["status"] = rng.choice(["N/A", "Unknown"])
                row_as_master["city"] = rng.choice(["N/A", "Unknown"])
            elif quirk == "mashed_zip":
                row_as_master["address"] = (
                    f"{row_as_master['address_line1']}, {city} {zip_code}"
                )
                row_as_master["postal_code"] = ""
            elif quirk == "typo_name":
                row_as_master["first_name"] = first.replace("h", "") if "h" in first else first + "n"
            elif quirk == "test_email":
                row_as_master["email"] = "test@test.com"
                row_as_master["first_name"] = "Test"
                row_as_master["last_name"] = "User"

        # Map onto requested columns (support both master and raw-ish headers)
        out: dict[str, str] = {}
        lower_map = {c.lower(): c for c in columns}
        for col in columns:
            key = col.lower().strip()
            if key in row_as_master:
                out[col] = str(row_as_master[key])
            elif key in {"name", "donor name", "full name"}:
                out[col] = row_as_master["full_name"]
            elif key in {"address", "mailing address"}:
                out[col] = row_as_master["address"]
            elif key.replace(" ", "_") in row_as_master:
                out[col] = str(row_as_master[key.replace(" ", "_")])
            else:
                out[col] = ""
        rows.append(out)

    # Append near-duplicate twins of early rows
    for t in range(min(twin_budget, len(rows))):
        base = dict(rows[t])
        # Mutate name slightly
        for col in list(base):
            cl = col.lower()
            if "first" in cl and base[col]:
                base[col] = base[col] + ("n" if not base[col].endswith("n") else "")
            if cl in {"name", "full name", "donor name"} and base[col]:
                base[col] = base[col].replace("Smith", "Smyth").replace("John", "Jon")
        rows.append(base)

    rng.shuffle(rows)
    return rows[:n] if len(rows) > n else rows


class SyntheticDataAgent:
    spec = AgentSpec(
        id="synthetic_data",
        name="Synthetic Data Generator",
        description=(
            "Generates privacy-safe dummy CRM rows that mimic client schema quirks "
            "for safe dedupe/migration testing before touching live exports."
        ),
        capabilities=["synthetic_data"],
        input_kinds=["schema_columns", "sample_csv"],
        output_kinds=["synthetic_csv", "quirk_report"],
        risks=["Synthetic data is not a substitute for UAT on a masked prod slice"],
    )

    def run(self, payload: dict[str, Any]) -> AgentResult:
        columns = payload.get("columns")
        if not columns and payload.get("csv_path"):
            columns = infer_schema_from_csv(payload["csv_path"])
        if not columns:
            columns = [
                "Donor Name",
                "Email Address",
                "Phone",
                "Mailing Address",
                "Status",
                "Notes",
            ]

        n = int(payload.get("row_count") or payload.get("n") or 1000)
        n = max(10, min(n, 10000))
        seed = payload.get("seed", 42)
        quirk_rate = float(payload.get("quirk_rate", 0.25))

        rows = generate_rows(columns, n, seed=seed, quirk_rate=quirk_rate)

        output_dir = Path(payload.get("output_dir") or "outputs/synthetic_data")
        output_dir.mkdir(parents=True, exist_ok=True)
        out_csv = output_dir / f"synthetic_{n}_rows.csv"
        quirk_path = output_dir / "quirk_report.json"

        with out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

        # Rough quirk stats
        empty_email = 0
        placeholders = 0
        for r in rows:
            blob = " ".join(str(v) for v in r.values()).lower()
            if any(k for k in r if "email" in k.lower() and not str(r[k]).strip()):
                empty_email += 1
            if any(p.lower() in blob for p in ("n/a", "unknown", "tbd", "test@test.com")):
                placeholders += 1

        report = {
            "row_count": len(rows),
            "columns": columns,
            "quirk_rate_requested": quirk_rate,
            "approx_empty_email_rows": empty_email,
            "approx_placeholder_rows": placeholders,
            "seed": seed,
        }
        quirk_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return AgentResult(
            ok=True,
            agent_id=self.spec.id,
            summary=f"Generated {len(rows)} synthetic rows across {len(columns)} columns.",
            artifacts={
                "synthetic_csv": str(out_csv),
                "quirk_report": str(quirk_path),
                "report": report,
            },
            next_agents=["schema_mapper", "fuzzy_match"],
        )
