"""
Schema Mapper & Normalization Agent

Feeds on raw CRM CSVs (Bloomerang / Kindful / Salesforce-ish exports), maps
messy headers onto a master donor schema, and writes a Python cleaner script
plus a normalized CSV.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from agents.base import AgentResult, AgentSpec

MASTER_SCHEMA = [
    "first_name",
    "last_name",
    "email",
    "phone",
    "address_line1",
    "city",
    "state",
    "postal_code",
    "status",
    "notes",
]

# Heuristic header aliases → master field
HEADER_ALIASES: dict[str, list[str]] = {
    "first_name": ["first", "firstname", "first name", "fname", "given name", "donor first"],
    "last_name": ["last", "lastname", "last name", "lname", "surname", "donor last"],
    "email": ["email", "e-mail", "email address", "donor email", "primary email"],
    "phone": ["phone", "mobile", "cell", "telephone", "primary phone"],
    "address_line1": ["address", "address1", "address line 1", "street", "street address", "addr"],
    "city": ["city", "town"],
    "state": ["state", "province", "region", "st"],
    "postal_code": ["zip", "zipcode", "zip code", "postal", "postal code", "postcode"],
    "status": ["status", "constituent status", "account status", "stage"],
    "notes": ["notes", "comment", "comments", "memo", "description"],
}

FULL_NAME_ALIASES = [
    "name",
    "full name",
    "donor name",
    "constituent name",
    "contact name",
]


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def map_headers(headers: list[str]) -> dict[str, Any]:
    """Map raw CSV headers onto MASTER_SCHEMA using aliases + full-name detection."""
    normalized = [_norm_header(h) for h in headers]
    mapping: dict[str, str | None] = {field: None for field in MASTER_SCHEMA}
    used: set[str] = set()
    transforms: list[dict[str, str]] = []

    # Direct alias matches
    for field, aliases in HEADER_ALIASES.items():
        for header, raw in zip(normalized, headers):
            if header in used:
                continue
            if header == field or header in aliases:
                mapping[field] = raw
                used.add(header)
                break

    # Combined full-name column → split to first/last
    if mapping["first_name"] is None or mapping["last_name"] is None:
        for header, raw in zip(normalized, headers):
            if header in used:
                continue
            if header in FULL_NAME_ALIASES or (
                "name" in header and "first" not in header and "last" not in header
            ):
                transforms.append(
                    {
                        "type": "split_full_name",
                        "source": raw,
                        "targets": "first_name,last_name",
                    }
                )
                if mapping["first_name"] is None:
                    mapping["first_name"] = f"{raw}→first"
                if mapping["last_name"] is None:
                    mapping["last_name"] = f"{raw}→last"
                used.add(header)
                break

    # Zip buried inside an address-like free-text column
    if mapping["postal_code"] is None:
        for header, raw in zip(normalized, headers):
            if header in used:
                continue
            if "address" in header or header in {"location", "mailing", "home"}:
                transforms.append(
                    {
                        "type": "extract_postal_from_address",
                        "source": raw,
                        "target": "postal_code",
                    }
                )
                mapping["postal_code"] = f"{raw}→postal"
                break

    unmapped = [h for h, n in zip(headers, normalized) if n not in used]
    return {
        "master_schema": MASTER_SCHEMA,
        "mapping": mapping,
        "transforms": transforms,
        "unmapped_source_columns": unmapped,
        "coverage": round(
            sum(1 for v in mapping.values() if v) / len(MASTER_SCHEMA), 3
        ),
    }


def _split_full_name(value: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (value or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _extract_zip(value: str) -> str:
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", value or "")
    return match.group(1) if match else ""


def normalize_rows(headers: list[str], rows: list[dict], plan: dict) -> list[dict]:
    mapping: dict[str, str | None] = plan["mapping"]
    transforms = plan.get("transforms") or []
    out: list[dict] = []

    for row in rows:
        record = {field: "" for field in MASTER_SCHEMA}
        for field, source in mapping.items():
            if not source or "→" in str(source):
                continue
            record[field] = (row.get(source) or "").strip()

        for t in transforms:
            if t["type"] == "split_full_name":
                first, last = _split_full_name(row.get(t["source"], ""))
                if not record["first_name"]:
                    record["first_name"] = first
                if not record["last_name"]:
                    record["last_name"] = last
            elif t["type"] == "extract_postal_from_address":
                if not record["postal_code"]:
                    record["postal_code"] = _extract_zip(row.get(t["source"], ""))
                    # Also try to keep street if address_line1 empty
                    if not record["address_line1"]:
                        street = re.sub(
                            r"\b\d{5}(?:-\d{4})?\b", "", row.get(t["source"], "")
                        ).strip(" ,")
                        record["address_line1"] = street

        # Soft placeholder cleanup
        for k, v in list(record.items()):
            if str(v).strip().lower() in {"n/a", "na", "unknown", "tbd", "none"}:
                record[k] = ""
        out.append(record)
    return out


def generate_cleaner_script(plan: dict, input_path: str, output_path: str) -> str:
    """Emit a standalone Python cleaner the consultancy can reuse per client."""
    return f'''#!/usr/bin/env python3
"""Auto-generated by Schema Mapper & Normalization Agent. Edit with care."""
import csv
import re
from pathlib import Path

PLAN = {json.dumps(plan, indent=2)}
INPUT_PATH = {input_path!r}
OUTPUT_PATH = {output_path!r}
MASTER = {json.dumps(MASTER_SCHEMA)}

def split_full_name(value):
    parts = [p for p in re.split(r"\\s+", (value or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])

def extract_zip(value):
    m = re.search(r"\\b(\\d{{5}})(?:-\\d{{4}})?\\b", value or "")
    return m.group(1) if m else ""

def main():
    with open(INPUT_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    mapping = PLAN["mapping"]
    transforms = PLAN.get("transforms") or []
    out_rows = []
    for row in rows:
        record = {{field: "" for field in MASTER}}
        for field, source in mapping.items():
            if not source or "→" in str(source):
                continue
            record[field] = (row.get(source) or "").strip()
        for t in transforms:
            if t["type"] == "split_full_name":
                first, last = split_full_name(row.get(t["source"], ""))
                record["first_name"] = record["first_name"] or first
                record["last_name"] = record["last_name"] or last
            elif t["type"] == "extract_postal_from_address":
                record["postal_code"] = record["postal_code"] or extract_zip(row.get(t["source"], ""))
        out_rows.append(record)
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"Wrote {{len(out_rows)}} normalized rows → {{OUTPUT_PATH}}")

if __name__ == "__main__":
    main()
'''


class SchemaMapperAgent:
    spec = AgentSpec(
        id="schema_mapper",
        name="Schema Mapper & Normalization Agent",
        description=(
            "Maps raw CRM CSV headers onto a master donor schema, splits mashed "
            "names, extracts buried postal codes, and writes a reusable cleaner script."
        ),
        capabilities=["schema_normalize"],
        input_kinds=["raw_crm_csv"],
        output_kinds=["normalized_csv", "cleaner_script", "mapping_plan"],
        risks=["Heuristic mis-map on novel headers — review coverage < 0.7"],
    )

    def run(self, payload: dict[str, Any]) -> AgentResult:
        csv_path = payload.get("csv_path")
        csv_text = payload.get("csv_text")
        output_dir = Path(payload.get("output_dir") or "outputs/schema_mapper")
        output_dir.mkdir(parents=True, exist_ok=True)

        if csv_path:
            path = Path(csv_path)
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                headers = list(reader.fieldnames or [])
                rows = list(reader)
            source_label = str(path)
        elif csv_text:
            import io

            reader = csv.DictReader(io.StringIO(csv_text))
            headers = list(reader.fieldnames or [])
            rows = list(reader)
            source_label = "inline_csv"
        else:
            return AgentResult(
                ok=False,
                agent_id=self.spec.id,
                summary="Provide csv_path or csv_text",
                errors=["missing_input"],
            )

        plan = map_headers(headers)
        normalized = normalize_rows(headers, rows, plan)

        normalized_path = output_dir / "normalized.csv"
        plan_path = output_dir / "mapping_plan.json"
        script_path = output_dir / "normalize_crm_export.py"

        with normalized_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MASTER_SCHEMA)
            writer.writeheader()
            writer.writerows(normalized)

        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        script = generate_cleaner_script(plan, source_label, str(normalized_path))
        script_path.write_text(script, encoding="utf-8")

        return AgentResult(
            ok=True,
            agent_id=self.spec.id,
            summary=(
                f"Mapped {len(headers)} source columns → master schema "
                f"(coverage {plan['coverage']}); wrote {len(normalized)} normalized rows."
            ),
            artifacts={
                "mapping_plan": plan,
                "normalized_csv": str(normalized_path),
                "cleaner_script": str(script_path),
                "plan_json": str(plan_path),
                "row_count": len(normalized),
            },
            next_agents=["fuzzy_match", "synthetic_data"],
        )
