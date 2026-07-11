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

If anything in steps 3–10 fails: Status is set to `Error` and the error message is written to the `Notes` property on the Notion page.

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

There is a single shared Notion database and a single shared Google Drive folder — no per-user or per-tenant credentials.

---

## Deploying to Render

Connect this repo in the Render dashboard. `render.yaml` handles the configuration.

Set all environment variables in the Render dashboard under **Environment** — they are intentionally marked `sync: false` (not stored in the repo). The service starts with:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

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

1. **Notion API integration webhook subscription** (recommended, catches pages moved into the DB) — configured on the integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) → **Webhooks**, subscribed to `page.created` and `page.moved`, pointed at `https://al-meeting-notes.onrender.com/webhook/notion`. Requires a one-time verification handshake (Notion POSTs a `verification_token`, which the endpoint logs — copy it from Render logs into the Notion UI to confirm).
2. **Notion database Automation** (legacy, doesn't reliably fire on moved-in pages) — Automations tab → trigger "Page added" or "Status is not set to Processing/Done/Error" → action "Send a webhook" to the same URL. Can run alongside the integration webhook as a backstop.

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
