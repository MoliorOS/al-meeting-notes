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
1.  Query the Meetings DB for pages with empty Status, created within
    POLL_LOOKBACK_HOURS — skip everything else
2.  For each candidate, fetch the Notion AI meeting page (title + attendees + AI summary blocks)
3.  Not ready yet (no summary blocks)? Leave Status empty, retry next cycle —
    unless POLL_AGE_OUT_HOURS has passed, then mark Status = Error and stop retrying
4.  Ready → mark Status = Processing
5.  Render blocks to plain text the LLM can read
6.  Claude extracts metadata: project, meeting type, date, attendees, next meeting
7.  Claude structures sections: headings → numbered items, action items flagged with owner
8.  Assemble the full document data object
9.  Generate DOCX from AL branded template (assets/template.docx)
10. Upload DOCX to Google Drive → get webViewLink
11. Mark Status = Done, write Drive URL to Document property
```

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
something older than `POLL_LOOKBACK_HOURS` that you still want processed).

---

## File structure

```
main.py                  # FastAPI app — poll loop, endpoints, the 11-step pipeline
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
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `NOTION_API_KEY` | Yes | Internal integration secret for the shared Meetings DB |
| `NOTION_MEETINGS_DB_ID` | Yes | Meetings database ID the poller queries — required for polling to do anything (logs a warning and stays idle without it) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Full Google service account JSON (as a string) |
| `GOOGLE_DRIVE_FOLDER_ID` | No | Drive folder to upload docs into — service skips upload if unset |
| `POLL_INTERVAL_SECONDS` | No (default `120`) | How often the poll loop checks the Meetings DB |
| `POLL_LOOKBACK_HOURS` | No (default `48`) | Ignore pages older than this — a backlog guard so turning polling on doesn't sweep up everything ever left unprocessed |
| `POLL_AGE_OUT_HOURS` | No (default `6`) | If a page still has no summary blocks after this long, give up and mark it `Error` instead of retrying forever |
| `NOTION_WEBHOOK_SECRET` | No | Unused on al-vps (nothing calls the webhook there). Only relevant if the Render deployment is ever brought back — see "The webhook path (unused, kept for Render rollback)" below. |

There is a single shared Notion database and a single shared Google Drive folder — no per-user or per-tenant credentials.

---

## Deploying

**Live deployment (since 2026-08-17): a single Docker container on `al-vps`** (AL's own
Hetzner VPS — see `MOLIOR-OS/projects/OS/al-vps/`), built from the `Dockerfile` in this repo
(non-root, read-only rootfs, all capabilities dropped) via `docker-compose.yml`. Bound to
`127.0.0.1:8000` only — **no public ingress of any kind**. The service polls Notion instead of
receiving a webhook, so it needs nothing exposed; al-vps's firewall stays `default deny incoming`
for every port, matching the host's original tailnet-only design. Env vars live in a `.env` (mode
600, gitignored) next to `docker-compose.yml` on the host, not in this repo.

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

The shared Meetings DB needs four properties:

| Property | Type | Purpose |
|---|---|---|
| `Name` (or `Title`) | Title | Page name — auto-set by Notion AI |
| `Status` | Select | Pipeline tracks state here: `Processing`, `Done`, `Error` |
| `Document` | URL | Drive link written back when processing completes |
| `Notes` | Rich text | Error messages written here if the pipeline fails |

Add the select options `Processing`, `Done`, `Error` to the `Status` property.

The service reads/writes through a single internal Notion integration (`NOTION_API_KEY`), added to the database via `...` → **Connections**.

No webhook or Automation needs to be configured — the service finds new pages itself by polling.
Just make sure `NOTION_MEETINGS_DB_ID` on the deployment matches this database's ID.

### Test it

Either create a real meeting in Notion and wait for the next poll cycle (Notion AI takes ~2 min to
write the summary; the poller checks every `POLL_INTERVAL_SECONDS`, default 2 min — so expect
Status to update within about 4 minutes worst case), or trigger a specific page immediately:

```
GET /manual?page_id=<page_id>
```

against wherever the service is reachable (over the tailnet on al-vps, or the Render URL if that
deployment is active). `page_id` is the 32-char hex ID of a specific meeting page (from its URL).

### The webhook path (unused, kept for Render rollback)

`main.py` still has `/webhook/notion` (GET verification handshake + POST receiver) and
`X-Notion-Signature` verification via `NOTION_WEBHOOK_SECRET`. Nothing calls it on al-vps. It only
matters if the Render deployment is ever reactivated:

1. **Notion API integration webhook subscription** — configured on the integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) → **Webhooks**, subscribed to `page.created` and `page.moved`, pointed at `/webhook/notion`. Requires a one-time verification handshake (Notion POSTs a `verification_token`, logged by the endpoint — copy it into the Notion UI to confirm, **and also set it as `NOTION_WEBHOOK_SECRET`** so subsequent events are signature-verified).
2. **Notion database Automation** (legacy) — Automations tab → trigger "Page added" → action "Send a webhook" to the same URL. Has no signing mechanism at all, so it 401s once `NOTION_WEBHOOK_SECRET` is set.

---

## Google Drive setup (optional)

Drive upload is optional — the service degrades gracefully if the Drive credentials are not set (the DOCX is generated but not saved anywhere persistent, and `drive_url` in the response will be `null`).

To enable:

1. Create a Google Cloud service account and download its JSON key.
2. Share the target Drive folder with the service account's email address (found in the JSON key as `client_email`).
3. Set `GOOGLE_SERVICE_ACCOUNT_JSON` in the deployment's env to the full JSON key content (as a string).
4. Set `GOOGLE_DRIVE_FOLDER_ID` to the folder ID from the Drive URL (`https://drive.google.com/drive/folders/<folder_id>`).

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

**Drive upload failing** — check that the service account's email still has access to the Drive folder, and that `GOOGLE_SERVICE_ACCOUNT_JSON` is valid. Drive errors do not fail the pipeline — Status is still set to Done, just without a Document URL.
