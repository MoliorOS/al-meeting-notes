# AL Meeting Notes Automation

**A Notion-native pipeline that turns a Notion AI meeting recording into a branded AL Word document — automatically.**

The service polls the AL Meetings database on a timer, reads the Notion AI summary from any
newly-appeared page, runs it through Claude, builds a formatted DOCX using AL's branded template,
uploads it to Google Drive, and writes the Drive link back into Notion. No one touches a template.
No one copies text.

Live service: a Docker container on `al-vps` (AL's own VPS — see `MOLIOR-OS/projects/OS/al-vps/`).

---

## How it works

```
Notion AI records meeting
         │
         ▼
Poll loop wakes up every POLL_INTERVAL_SECONDS (default 2 min)
         │
         ▼
0.  Repeat steps 1-11 below once per target in targets.json — each target is
    an independent {name, notion_db_id, google_drive_folder_id}; one target
    failing (DB not shared yet, bad ID) is logged and skipped, never blocks
    the others
1.  Query that target's Meetings DB for pages with empty Status, created
    within POLL_LOOKBACK_HOURS — skip everything else
2.  For each candidate, fetch the Notion AI meeting page (title + attendees + AI summary blocks)
3.  Not ready yet (no summary blocks)? Leave Status empty, retry next cycle —
    unless POLL_AGE_OUT_HOURS has passed, then mark Status = Error and stop retrying
4.  Ready → mark Status = Processing
5.  Render blocks to plain text the LLM can read
6.  Claude extracts metadata: project, meeting type, date, attendees, next meeting
7.  Claude structures sections: headings → numbered items, action items flagged with owner
8.  Assemble the full document data object
9.  Generate DOCX from AL branded template (assets/template.docx)
10. Upload DOCX to that target's Google Drive folder → get webViewLink
11. Mark Status = Done, write Drive URL to Document property
```

**Multi-tenant:** one container polls every target listed in `targets.json`, each pointed at its
own Notion database and Drive folder, sharing the same Anthropic key, Notion integration, and
Drive service account. See "Adding a new target" below — it's a config edit, not a redeploy.

If anything from step 4 onward fails, Status is set to `Error` and the error message is written to
the `Notes` property on the Notion page (best-effort: if Notion is unreachable enough that even
that write fails, the page is left as-is for a retry).

**Why polling, not a webhook:** the original design used a Notion webhook subscription. When the
service moved from Render (which gives every deployment a public URL for free) to al-vps
(deliberately tailnet-only, no public ingress), Notion's webhook sender turned out to reject the
Tailscale Funnel `*.ts.net` domain outright — confirmed with a `502` and zero request ever
reaching the container, reproduced identically across three different Funnel ports including the
default 443, with Funnel's own reachability independently confirmed working for every other
client. Tailscale Funnel has no way to serve a custom (non-`ts.net`) domain, so there was no fix
available inside that model. Polling sidesteps the problem entirely — the service calls out to
Notion instead of needing to be called in, so al-vps needs zero public exposure. See
`MOLIOR-OS/projects/OS/al-vps/al-meeting-notes-migration-PLAN.md` for the full investigation.
The webhook code (below) is kept in the repo, unused, as what makes a Render rollback possible.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/webhook/notion` | Notion webhook verification handshake (returns 200 OK) — unused on al-vps, kept for the Render rollback path |
| `POST` | `/webhook/notion` | Main webhook receiver — unused on al-vps, same reason |
| `GET` | `/manual?page_id=<id>` | Manually trigger the pipeline for a specific page |

The `/manual` endpoint is also the fallback for any page the poller's lookback window skips (e.g.
something older than `POLL_LOOKBACK_HOURS` that you still want processed). It resolves which
target (and therefore which Drive folder) a page belongs to by looking up the page's parent
database ID against `targets.json` — the page must live in a database listed there, or `/manual`
returns a 400.

`/health` also lists the currently loaded target names, so a quick `curl` confirms
`targets.json` was picked up after a restart.

---

## File structure

```
main.py                  # FastAPI app — poll loop, endpoints, the 11-step pipeline
targets.py                # Loads + validates targets.json (multi-tenant config)
targets.json.example      # Template — copy to targets.json, not tracked by git
llm_pipeline.py          # Claude calls: extract_metadata() and structure_sections()
notion_utils.py          # Notion API helpers (fetch, render, status updates)
drive.py                 # Google Drive upload via a service account
scripts/
  generate_docx.py       # Fill AL branded template with meeting data → .docx
assets/
  template.docx          # AL branded Word template (3 tables: header, content, actions)
  al_logo.jpg            # AL logo (embedded in generated documents)
render.yaml              # Render deployment config
requirements.txt
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key — shared across every target |
| `NOTION_API_KEY` | Yes | Internal integration secret — shared across every target; each target's database must be individually shared with this integration (see "Adding a new target" below) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Full Google service account JSON (as a string) — shared across every target; each target's Drive folder must be individually shared with this service account |
| `TARGETS_FILE` | No (default `targets.json`) | Path to the multi-tenant config file — see "Adding a new target" below |
| `POLL_INTERVAL_SECONDS` | No (default `120`) | How often the poll loop checks every target's DB |
| `POLL_LOOKBACK_HOURS` | No (default `48`) | Ignore pages older than this — a backlog guard so turning polling on doesn't sweep up everything ever left unprocessed |
| `POLL_AGE_OUT_HOURS` | No (default `6`) | If a page still has no summary blocks after this long, give up and mark it `Error` instead of retrying forever |
| `NOTION_WEBHOOK_SECRET` | No | Unused on al-vps (nothing calls the webhook there). Only relevant if the Render deployment is ever brought back — see "The webhook path (unused, kept for Render rollback)" below. |

Credentials are shared; **which Notion database to poll and which Drive folder to upload into are
per-target**, defined in `targets.json` (gitignored, host-side config — not this table). If that
file is missing or empty, the poll loop logs a warning and stays idle, same as the old
`NOTION_MEETINGS_DB_ID` behavior.

---

## Deploying

**Live deployment (since 2026-08-17): a single Docker container on `al-vps`** (AL's own
Hetzner VPS — see `MOLIOR-OS/projects/OS/al-vps/`), built from the `Dockerfile` in this repo
(non-root, read-only rootfs, all capabilities dropped) via `docker-compose.yml`. Bound to
`127.0.0.1:8000` only — **no public ingress of any kind**. The service polls Notion instead of
receiving a webhook, so it needs nothing exposed; al-vps's firewall stays `default deny incoming`
for every port, matching the host's original tailnet-only design. Env vars live in a `.env` (mode
600, gitignored) next to `docker-compose.yml` on the host, not in this repo. `targets.json` (also
gitignored, host-side) is bind-mounted read-only into the container — see "Adding a new target"
above; a config edit there only needs `docker compose restart`, never a rebuild.

`render.yaml` is kept for reference/rollback — the service ran on Render (`starter` plan) before
this migration, and briefly on al-vps behind a Tailscale Funnel URL before that approach was
abandoned (Notion's webhook sender doesn't reach `*.ts.net` domains — see "How it works" above).
If polling ever needs to be reverted to the webhook model, Render is the fastest path back since
it gives every deployment a public URL for free; reconnect the repo in the Render dashboard and
restore `ANTHROPIC_API_KEY`, `NOTION_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`GOOGLE_DRIVE_FOLDER_ID` (skip the poll-only and `NOTION_WEBHOOK_SECRET` vars unless re-enabling
signature verification too).

---

## Notion setup

Every target's database needs the same four properties as the original Meetings DB:

| Property | Type | Purpose |
|---|---|---|
| `Name` (or `Title`) | Title | Page name — auto-set by Notion AI |
| `Status` | Select | Pipeline tracks state here: `Processing`, `Done`, `Error` |
| `Document` | URL | Drive link written back when processing completes |
| `Notes` | Rich text | Error messages written here if the pipeline fails |

Add the select options `Processing`, `Done`, `Error` to the `Status` property.

The service reads/writes through a single internal Notion integration (`NOTION_API_KEY`), added to
each target database individually via `...` → **Connections** — the integration doesn't
auto-see new databases just because it exists.

No webhook or Automation needs to be configured — the service finds new pages itself by polling.

### Adding a new target

The whole point of `targets.json` is that this is a config change, not a code change:

1. **Duplicate the Meetings DB** in Notion (`...` → **Duplicate**) rather than building a fresh
   database from scratch — this guarantees the schema (property names, `Status` select options)
   matches exactly, which the pipeline depends on.
2. Share the new database with the **AL Notion Automations** integration (`...` → **Connections**
   → add the integration).
3. Create a new Google Drive folder and share it with the service account's `client_email` (from
   `GOOGLE_SERVICE_ACCOUNT_JSON` — currently `al-notion-gws@molior-gws.iam.gserviceaccount.com`),
   **Editor** access.
4. On the host, append one entry to `targets.json`:
   ```json
   { "name": "<short-label-for-logs>", "notion_db_id": "<32-char db id>", "google_drive_folder_id": "<folder id from its Drive URL>" }
   ```
5. `docker compose restart` — no rebuild needed, `targets.json` is bind-mounted read-only. `GET
   /health` should list the new target name; the container logs a `[poll] starting: ... targets=[...]`
   line naming it too.
6. Verify end to end with `/manual?page_id=<a real page in the new DB>` before trusting the next
   live poll cycle.

A malformed `targets.json` (missing keys, a duplicate name or database ID) fails loudly at
container startup rather than silently dropping a target — check `docker logs al-meeting-notes`
if the container doesn't come up after an edit.

### Test it

Either create a real meeting in Notion and wait for the next poll cycle (Notion AI takes ~2 min to
write the summary; the poller checks every `POLL_INTERVAL_SECONDS`, default 2 min — so expect
Status to update within about 4 minutes worst case), or trigger a specific page immediately:

```
GET /manual?page_id=<page_id>
```

against wherever the service is reachable (over the tailnet on al-vps, or the Render URL if that
deployment is active). `page_id` is the 32-char hex ID of a specific meeting page (from its URL).
The page must belong to a database listed in `targets.json`, or `/manual` returns a 400.

### The webhook path (unused, kept for Render rollback)

`main.py` still has `/webhook/notion` (GET verification handshake + POST receiver) and
`X-Notion-Signature` verification via `NOTION_WEBHOOK_SECRET`. Nothing calls it on al-vps. It only
matters if the Render deployment is ever reactivated:

1. **Notion API integration webhook subscription** — configured on the integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) → **Webhooks**, subscribed to `page.created` and `page.moved`, pointed at `/webhook/notion`. Requires a one-time verification handshake (Notion POSTs a `verification_token`, logged by the endpoint — copy it into the Notion UI to confirm, **and also set it as `NOTION_WEBHOOK_SECRET`** so subsequent events are signature-verified).
2. **Notion database Automation** (legacy) — Automations tab → trigger "Page added" → action "Send a webhook" to the same URL. Has no signing mechanism at all, so it 401s once `NOTION_WEBHOOK_SECRET` is set.

---

## Google Drive setup

One service account (`GOOGLE_SERVICE_ACCOUNT_JSON`) is shared across every target; each target's
own folder just needs to be shared with that service account's email individually. If the JSON
key is missing entirely, the service degrades gracefully — the DOCX is still generated, just not
uploaded, and `drive_url` in the response is `null`. A per-target `google_drive_folder_id` that
was never shared with the service account fails only that target's uploads (logged, `Status` still
reaches `Done` with no `Document` link — see Troubleshooting).

To add the first (or an additional) folder:

1. Create the Drive folder.
2. Share it with the service account's email address (found in the JSON key as `client_email`).
3. Add its folder ID (from the Drive URL, `https://drive.google.com/drive/folders/<folder_id>`)
   to the corresponding entry in `targets.json` — see "Adding a new target" above.

---

## Production swaps

| Prototype | Production |
|---|---|
| Google Drive (`drive.py`) | Egnyte API — swap the module, same `upload_to_drive(local_path, filename)` interface |
| Claude Haiku | Can upgrade to a more capable model in `llm_pipeline.py → _MODEL` |

---

## Troubleshooting

**Status stuck on Processing** — the pipeline crashed before it could write Error. Check `docker logs al-meeting-notes` on al-vps for the page ID. Use `/manual?page_id=...` to retry once the underlying issue is fixed — the pipeline checks status at the start and will re-run since the page never reached Done.

**Page never picked up, Status stays empty** — most likely it's outside `POLL_LOOKBACK_HOURS` (default 48h — anything created before that is deliberately ignored to avoid a bulk backlog sweep). Use `/manual?page_id=...` to process it directly regardless of age.

**Status = Error, Notes says "Gave up after Nh waiting for Notion AI summary"** — the page never got summary blocks within `POLL_AGE_OUT_HOURS` (default 6h), usually because it's a placeholder page Notion AI never finished (or never started) writing to. Check the page in Notion; if it's a real meeting waiting on a slow AI summary, hit `/manual?page_id=...` once the summary appears.

**Poll loop seems dead** — `docker logs al-meeting-notes` should show a `[poll] cycle complete` line every `POLL_INTERVAL_SECONDS`. If it stops, check for a `[poll] cycle failed:` line just before — the loop always survives one bad cycle and retries, so a gap means the container itself needs a look (`docker compose restart`).

**Drive upload failing** — check that the service account's email still has access to that target's Drive folder, and that `GOOGLE_SERVICE_ACCOUNT_JSON` is valid. Drive errors do not fail the pipeline — Status is still set to Done, just without a Document URL.

**One target never processes anything, others are fine** — almost always the Notion integration hasn't been shared with that target's database yet (`...` → **Connections** on the database). Check `docker logs al-meeting-notes` for a `[poll:<name>] cycle failed:` line naming the target.

**`/manual?page_id=...` returns 400 "does not belong to any configured target"** — the page's parent database isn't listed in `targets.json`. Either it's a genuinely new database that needs onboarding (see "Adding a new target"), or the `notion_db_id` in `targets.json` has a typo.
