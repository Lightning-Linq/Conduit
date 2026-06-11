"""json-csv — convert between a JSON array of objects and CSV text."""

from __future__ import annotations

import csv
import io

from app.registry import Skill, SkillError, register


def run(input_data: dict) -> dict:
    direction = str(input_data.get("direction") or "").lower()
    if direction == "json_to_csv":
        rows = input_data.get("data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SkillError("`data` must be a list of objects for json_to_csv")
        if not rows:
            return {"csv": ""}
        fields = list(dict.fromkeys(key for row in rows for key in row))
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return {"csv": buffer.getvalue()}
    if direction == "csv_to_json":
        text = input_data.get("csv")
        if not isinstance(text, str):
            raise SkillError("`csv` (string) is required for csv_to_json")
        return {"data": list(csv.DictReader(io.StringIO(text)))}
    raise SkillError("`direction` must be 'json_to_csv' or 'csv_to_json'")


register(
    Skill(
        name="json-csv",
        description="Convert a JSON array of objects to CSV text, or CSV back to JSON.",
        handler=run,
        input_example={"direction": "json_to_csv", "data": [{"name": "alice", "sats": 21}]},
    )
)
