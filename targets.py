"""
Multi-tenant target config — one {name, notion_db_id, google_drive_folder_id}
entry per Notion database this service polls. Credentials (Anthropic key,
Notion integration token, Drive service account) stay shared across every
target in .env; only the DB to poll and the Drive folder to upload into vary
per target.

Lives in a JSON file (default `targets.json`, override via TARGETS_FILE) so
adding a tenant is an edit + container restart — no rebuild, no code change.
See targets.json.example for the shape and README.md for the onboarding
checklist (duplicate the Meetings DB, share it + a new Drive folder, add one
entry here).
"""

import json
import os
from pathlib import Path

_TARGETS_FILE = os.environ.get("TARGETS_FILE", "targets.json")
_REQUIRED_KEYS = {"name", "notion_db_id", "google_drive_folder_id"}


def load_targets(path: str | None = None) -> list[dict]:
    """
    Load and validate the target list. Raises ValueError on a malformed
    file (missing keys, duplicate names/db ids) so misconfiguration fails
    loudly at startup rather than silently skipping a tenant at poll time.
    Returns [] if the file doesn't exist (polling disabled, same as the
    old "NOTION_MEETINGS_DB_ID not set" behavior).
    """
    file_path = Path(path or _TARGETS_FILE)
    if not file_path.exists():
        return []

    raw = json.loads(file_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{file_path}: expected a JSON array of target objects")

    seen_names: set[str] = set()
    seen_dbs: set[str] = set()
    targets = []

    for i, entry in enumerate(raw):
        missing = _REQUIRED_KEYS - entry.keys()
        if missing:
            raise ValueError(f"{file_path}[{i}]: missing required key(s) {missing}")

        name = entry["name"]
        db_id = entry["notion_db_id"]
        if name in seen_names:
            raise ValueError(f"{file_path}[{i}]: duplicate target name '{name}'")
        if db_id in seen_dbs:
            raise ValueError(f"{file_path}[{i}]: duplicate notion_db_id '{db_id}' (target '{name}')")
        seen_names.add(name)
        seen_dbs.add(db_id)

        targets.append({
            "name": name,
            "notion_db_id": db_id,
            "google_drive_folder_id": entry["google_drive_folder_id"],
        })

    return targets
