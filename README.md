# AL Meeting Notes Automation

**A Notion-native pipeline that turns a Notion AI meeting recording into a branded AL Word document — automatically.**

When a new meeting recording page appears in the AL Meetings database, the service wakes up, reads the Notion AI summary, runs it through Claude, builds a formatted DOCX using AL's branded template, uploads it to Google Drive, and writes the Drive link back into Notion. No one touches a template. No one copies text.

Live service: `https://al-meeting-notes.onrender.com`

---

## How it works

```
Notion AI records meeting
         │
         ▼
Notion Automation fires webhook → POST /webhook/notion
         │
         ▼
1.  Read Status — skip if already Processing or Done
2.  Mark Status = Processing
3.  Fetch the Notion AI meeting page (title + attendees + AI summary blocks)
4.  Render blocks to plain text the LLM can read
5.  Claude extracts metadata: project, meeting type, date, attendees, next meeting
6.  Claude structures sections: headings → numbered items, action items flagged with owner
7.  Assemble the full document data object
8.  Generate DOCX from AL branded template (assets/template.docx)
9.  Upload DOCX to Google Drive → get webViewLink
10. Mark Status = Done, write Drive URL to Document property
```

If anything in steps 1–10 fails — including the initial status check or the Processing write itself — Status is set to `Error` and the error message is written to the `Notes` property on the Notion page (best-effort: if Notion is unreachable enough that even that write fails, the page is left as-is for a retry).

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/webhook/notion` | Notion webhook verification handshake (returns 200 OK) |
| `POST` | `/webhook/notion` | Main webhook receiver — processes one meeting page |
| `GET` | `/manual?page_id=<id>` | Manually trigger the pipeline for a specific page |

The `/manual` endpoint is the fallback for any page that slips through without a webhook event — it runs the exact same pipeline as the webhook path.

---

## File structure

```
main.py                  # FastAPI app — all endpoints + the 10-step pipeline
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
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Full Google service account JSON (as a string) |
| `GOOGLE_DRIVE_FOLDER_ID` | No | Drive folder to upload docs into — service skips upload if unset |
| `NOTION_WEBHOOK_SECRET` | No | The `verification_token` from the integration webhook subscription's handshake (see "Notion setup" below). Once set, every `/webhook/notion` POST except the handshake itself must carry a valid `X-Notion-Signature`, or it's rejected with 401 — closes off the endpoint to anyone who just guesses/finds the URL. Leave unset only while doing local/manual testing; the legacy database-Automation "Send a webhook" path has no signing mechanism at all and will start getting 401'd once this is set (see caveat in "Notion setup"). |

There is a single shared Notion database and a single shared Google Drive folder — no per-user or per-tenant credentials.

---

## Deploying

**Live deployment (since 2026-08-17): a single Docker container on `al-vps`** (AL's own
Hetzner VPS — see `MOLIOR-OS/projects/OS/al-vps/`), built from the `Dockerfile` in this repo
(non-root, read-only rootfs, all capabilities dropped) via `docker-compose.yml`. Exposed to
the public internet — required for Notion's webhook to reach it — via **Tailscale Funnel**
(`tailscale funnel --bg --https=8443 http://127.0.0.1:8000`), not a published port; the host's
firewall stays `default deny incoming` for everything else. Env vars live in a `.env` (mode
600, gitignored) next to `docker-compose.yml` on the host, not in this repo.

`render.yaml` is kept for reference/rollback — the service ran on Render (`starter` plan)
until this migration; that deployment can be resumed by reconnecting the repo in the Render
dashboard and restoring the same four env vars (`NOTION_WEBHOOK_SECRET` is new, al-vps-only,
skip it there since Render never had signature verification in the first place).

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

Two mechanisms feed the webhook:

1. **Notion API integration webhook subscription** (recommended, catches pages moved into the DB) — configured on the integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) → **Webhooks**, subscribed to `page.created` and `page.moved`, pointed at the service's `/webhook/notion` URL. Requires a one-time verification handshake (Notion POSTs a `verification_token`, which the endpoint logs — copy it from the container logs into the Notion UI to confirm, **and also set it as `NOTION_WEBHOOK_SECRET`** so subsequent events are signature-verified).
2. **Notion database Automation** (legacy, doesn't reliably fire on moved-in pages) — Automations tab → trigger "Page added" or "Status is not set to Processing/Done/Error" → action "Send a webhook" to the same URL. Can run alongside the integration webhook as a backstop **only while `NOTION_WEBHOOK_SECRET` is unset** — Notion's legacy Automation webhook has no signing mechanism, so once signature verification is turned on this path gets rejected with 401. Decide whether the backstop is worth leaving the endpoint acceptable to any correctly-shaped unsigned POST, or drop it once the signed subscription is confirmed reliable.

### Test it

Either create a real meeting in Notion (let Notion AI process it, wait ~2 min, then check Status) or trigger manually:

```
GET https://al-meeting-notes.onrender.com/manual?page_id=<page_id>
```

`page_id` is the 32-char hex ID of a specific meeting page (from its URL).

---

## Google Drive setup (optional)

Drive upload is optional — the service degrades gracefully if the Drive credentials are not set (the DOCX is generated but not saved anywhere persistent, and `drive_url` in the response will be `null`).

To enable:

1. Create a Google Cloud service account and download its JSON key.
2. Share the target Drive folder with the service account's email address (found in the JSON key as `client_email`).
3. Set `GOOGLE_SERVICE_ACCOUNT_JSON` in Render to the full JSON key content (as a string).
4. Set `GOOGLE_DRIVE_FOLDER_ID` to the folder ID from the Drive URL (`https://drive.google.com/drive/folders/<folder_id>`).

---

## Production swaps

| Prototype | Production |
|---|---|
| Google Drive (`drive.py`) | Egnyte API — swap the module, same `upload_to_drive(local_path, filename)` interface |
| Claude Haiku | Can upgrade to a more capable model in `llm_pipeline.py → _MODEL` |

---

## Troubleshooting

**Status stuck on Processing** — the pipeline crashed before it could write Error. Check Render logs for the page ID. Use `/manual?page_id=...` to retry once the underlying issue is fixed — the pipeline checks status at the start and will re-run since the page never reached Done.

**Status = Error, Notes says "Meeting page has no summary blocks"** — Notion AI was still generating when the webhook fired. Wait a minute and hit `/manual?page_id=...` to retry.

**Webhook not firing** — confirm the Automation/webhook subscription is enabled and the integration has access to the database. Check Render logs for incoming POST requests to `/webhook/notion`.

**Drive upload failing** — check that the service account's email still has access to the Drive folder, and that `GOOGLE_SERVICE_ACCOUNT_JSON` is valid. Drive errors do not fail the pipeline — Status is still set to Done, just without a Document URL.
