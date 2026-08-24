---
description: Onboard a new tenant (Notion meetings DB + Drive folder) into the multi-tenant al-meeting-notes automation on al-vps
argument-hint: [notion db url] [drive folder url] [short target name]
allowed-tools: Bash, Read, Edit
---

# /add-source

Adds one new tenant to the live `al-meeting-notes` automation — a Notion "Meetings" database
paired with a Google Drive folder — without touching code or rebuilding the container. This
mirrors the multi-tenant design in `targets.py`/`README.md` ("Adding a new target"): credentials
(Anthropic key, Notion integration, Drive service account) are already shared across every
target; onboarding a new one is purely a `targets.json` config edit + restart.

If `$ARGUMENTS` supplies the Notion DB URL, Drive folder URL, and a short name, use them
directly. Otherwise ask the operator for all three before starting — don't guess a name.

## Step 0 — Confirm the live deployment location

This command assumes the live container is on `al-vps` at `/opt/al-meeting-notes`, reachable via
`ssh al-vps`. If that host/path is wrong for this environment, stop and ask rather than guessing
a different target.

## Step 1 — Verify the Notion database schema

Fetch the new database (via whatever Notion access this session has — MCP fetch tool if
connected, otherwise ask the operator to confirm) and check it has exactly these properties,
matching the canonical Meetings DB:

| Property | Type | Notes |
|---|---|---|
| `Meeting` (or `Name`/`Title`) | Title | — |
| `Date` | Date | — |
| `Attendees` | Text | — |
| `Document` | URL | — |
| `Status` | Select | Options must be exactly `Processing`, `Done`, `Error` |

If the operator says the new DB was created by **duplicating** the Meetings DB, this is
guaranteed and you can skip a full property-by-property check — just confirm the `Status` select
options match. If it was hand-built, verify all five properties explicitly; a schema mismatch
will surface as silent failures later, not a clean error at onboarding time.

## Step 2 — Connect the Notion integration to the new database

The database needs the shared **AL Notion Automations** integration added as a connection —
internal integrations don't automatically see new databases just because they exist.

If a connected browser is available: navigate to the database page, open the `...` menu (top
right) → **Connections** → **Add connection** → search "AL Notion Automations" → **Add to
page**. Confirm the connection count incremented.

If no browser is available, tell the operator to do this themselves (it's a ~10-second UI
action, no API path exists for it) and wait for confirmation before continuing.

**Watch for stray pages:** clicking around an empty database table view can accidentally create
a blank row/page. If that happens, delete it (`...` → **Move to Trash** on the page) before
finishing — don't leave test junk in the operator's real database.

## Step 3 — Verify the Drive folder is shared

Ask the operator to share the new Drive folder with the service account's `client_email` (from
`GOOGLE_SERVICE_ACCOUNT_JSON` on al-vps — currently
`al-notion-gws@molior-gws.iam.gserviceaccount.com`) if they haven't already. **Editor** on a
regular folder or **Contributor** on a Shared Drive both work — the pipeline only ever creates
files, never deletes/moves/shares, so neither restricts it.

Once shared, verify with a live round-trip test using the exact code path the pipeline uses
(don't just trust "I shared it" — confirm it, the way this was confirmed for `admin-meetings` on
2026-08-24):

```bash
ssh al-vps "docker exec al-meeting-notes python3 -c \"
from drive import upload_to_drive
import tempfile
with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
    tmp.write(b'access test')
    path = tmp.name
print(upload_to_drive(path, 'ZZZ_add-source-access-test.docx', folder_id='<drive_folder_id>'))
\""
```

A returned URL confirms write access. **This leaves a real test file in the operator's Drive
folder** — tell them its exact name (`ZZZ_add-source-access-test.docx`) so they can delete it;
if the folder is a Shared Drive with delete restrictions (seen on `admin-meetings`'s folder), you
may not be able to delete it yourself even though you created it — don't assume you can, check
by attempting a `files().delete()` call and report if it 404s/403s instead of silently leaving
the file unmentioned.

## Step 4 — Add the target to `targets.json`

```bash
ssh al-vps "cat /opt/al-meeting-notes/targets.json"
```

Read the current file, then write it back with one new entry appended (don't hand-edit with
`sed` — round-trip through a JSON-aware edit to avoid a syntax error that would crash the
container on restart):

```json
{
  "name": "<short-label-for-logs>",
  "notion_db_id": "<32-char db id, dashes optional>",
  "google_drive_folder_id": "<folder id from its Drive URL>"
}
```

Validate before pushing it to the host:

```bash
python3 -c "import json; json.load(open('targets.json'))" # locally, on the edited copy
```

Then write it to `al-vps:/opt/al-meeting-notes/targets.json` (via `scp` or a heredoc over `ssh`).

## Step 5 — Restart and verify

No rebuild needed — `targets.json` is bind-mounted read-only:

```bash
ssh al-vps "cd /opt/al-meeting-notes && docker compose restart"
```

Then confirm:

1. `curl http://127.0.0.1:8000/health` (via `ssh al-vps`) lists the new target name.
2. `docker logs al-meeting-notes --tail 20` shows a `[poll] starting: ... targets=[..., <new
   name>]` line and no `[poll:<new name>] cycle failed: Could not find database` error (that
   error means Step 2 wasn't actually completed — go back and check the connection).
3. Wait for the next poll cycle (or trigger `/manual?page_id=<a real page in the new DB>` if one
   exists) and confirm it reaches `Status = Done` with a `Document` link written back.

## Step 6 — Report back

Tell the operator: what target name was added, confirmation both Notion and Drive access are
live (not just "should be" — cite the actual test results from Steps 2/3/5), and the exact name
of any leftover test file they need to clean up themselves.
