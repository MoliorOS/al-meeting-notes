import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from drive import upload_to_drive
from llm_pipeline import extract_metadata, structure_sections
from notion_utils import (
    extract_page_text,
    fetch_meeting_page,
    get_page_database_id,
    mark_done,
    mark_error,
    mark_processing,
    read_meeting_status,
)
from scripts.generate_docx import generate_docx

# {"<db_id_no_dashes>": {"api_key": "secret_...", "name": "Jon"}, ...}
_TENANTS: dict = json.loads(os.environ.get("NOTION_TENANTS", "{}"))


class MeetingNotReadyError(Exception):
    """Notion AI hasn't finished generating the summary yet — safe for Notion to retry."""


def _normalize_id(id_str: str) -> str:
    return id_str.replace("-", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("AL Meeting Notes service starting...")
    yield


app = FastAPI(title="AL Meeting Notes Automation", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "al-meeting-notes"}


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def process_meeting(page_id: str, api_key: str | None = None) -> dict:
    """
    Full pipeline for one meeting page:

    1. Check status — skip if already Done or Processing.
    2. Mark as Processing.
    3. Fetch the Notion AI meeting recording page.
    4. Render blocks to LLM-readable text.
    5. Extract metadata (LLM).
    6. Structure sections (LLM).
    7. Assemble final JSON.
    8. Generate DOCX from template.
    9. Upload to Google Drive.
    10. Mark as Done with Drive URL.
    """
    status = read_meeting_status(page_id, api_key=api_key)
    if status in ("Done", "Processing"):
        return {"status": "skipped", "reason": f"page is already {status}"}

    mark_processing(page_id, api_key=api_key)

    try:
        page_data = fetch_meeting_page(page_id, api_key=api_key)

        if not page_data["blocks"]:
            raise MeetingNotReadyError("Meeting page has no summary blocks — Notion AI may still be processing.")

        page_text = extract_page_text(page_data)

        metadata = extract_metadata(page_text)
        sections = structure_sections(page_text)

        doc_data = {**metadata, "sections": sections}

        slug = re.sub(r"[^\w]", "", (metadata.get("project") or "Meeting").replace(" ", ""))[:30]
        date_slug = (metadata.get("date") or "").replace(" ", "")
        filename = f"MeetingNotes_{slug}_{date_slug}.docx"

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = tmp.name

        generate_docx(doc_data, Path(tmp_path))
        drive_url = upload_to_drive(tmp_path, filename)

        mark_done(page_id, drive_url, api_key=api_key)

        return {
            "status": "done",
            "meeting": metadata.get("meeting_type", ""),
            "project": metadata.get("project", ""),
            "sections": len(sections),
            "filename": filename,
            "drive_url": drive_url,
        }

    except Exception as e:
        error_msg = str(e)
        print(f"[pipeline] Error processing {page_id}: {error_msg}")
        try:
            mark_error(page_id, error_msg, api_key=api_key)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Webhook — fires on:
#   - legacy Notion database Automation ("Send webhook" action), payload shape
#     {"source": {...}, "data": {<page object>}}
#   - Notion API integration webhook subscription (e.g. page.moved, page.created),
#     payload shape {"type": "page.moved", "entity": {"id": "...", "type": "page"}, ...}
#   - the one-time subscription verification handshake {"verification_token": "..."}
# ---------------------------------------------------------------------------

# Event types we act on from an integration-webhook subscription. Others
# (page.content_updated, page.properties_updated, ...) are ignored — they'd
# otherwise fire repeatedly while Notion AI is still writing the summary.
_SUBSCRIBED_EVENT_TYPES = {"page.moved", "page.created"}


@app.get("/webhook/notion")
async def webhook_notion_verify():
    return {"status": "ok"}


def _extract_page_and_db_id(body: dict) -> tuple[str | None, str | None]:
    """Return (page_id, raw_db_id_or_None) from either payload shape. raw_db_id
    may be None even when page_id is present — the integration-webhook event
    envelope doesn't always carry the parent database id, so the caller must
    be prepared to look it up via the Notion API instead."""
    event_type = body.get("type")
    if event_type is not None:
        if event_type not in _SUBSCRIBED_EVENT_TYPES:
            return None, None
        entity = body.get("entity") or {}
        if entity.get("type") != "page":
            return None, None
        page_id = entity.get("id")
        raw_db_id = ((body.get("data") or {}).get("parent") or {}).get("database_id")
        return page_id, raw_db_id

    # Legacy database-automation payload
    data = body.get("data", {})
    page_id = data.get("id")
    raw_db_id = (data.get("parent") or {}).get("database_id")
    return page_id, raw_db_id


@app.post("/webhook/notion")
async def webhook_notion(request: Request):
    body = await request.json()
    print(f"[webhook/notion] payload: {body}")

    if "verification_token" in body:
        print(f"[webhook/notion] verification_token: {body['verification_token']}")
        return JSONResponse({"status": "ok"})

    page_id, raw_db_id = _extract_page_and_db_id(body)

    if not page_id:
        return JSONResponse({"status": "skipped", "reason": "no page id in payload"})

    if not raw_db_id:
        # Integration-webhook events don't always include the parent database id —
        # look it up directly so we can still resolve the right tenant.
        raw_db_id = get_page_database_id(page_id, api_key=os.environ.get("NOTION_API_KEY"))

    db_id = _normalize_id(raw_db_id or "")

    tenant = _TENANTS.get(db_id)
    if not tenant:
        print(f"[webhook/notion] unknown db_id: {db_id!r} — check NOTION_TENANTS")
        return JSONResponse(
            {"status": "skipped", "reason": f"unknown database {db_id}"},
            status_code=400,
        )

    api_key = tenant.get("api_key") or os.environ.get("NOTION_API_KEY")

    try:
        return JSONResponse(process_meeting(page_id, api_key=api_key))
    except MeetingNotReadyError as e:
        # Notion AI still generating — return 503 so Notion Automations retries
        print(f"[webhook/notion] retryable on {page_id}: {e}")
        return JSONResponse({"status": "retry", "reason": str(e)}, status_code=503)
    except Exception as e:
        print(f"[webhook/notion] error on {page_id}: {e}")
        return JSONResponse(
            {"status": "error", "page_id": page_id, "reason": str(e)},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Manual trigger — for testing or missed webhook events
# ---------------------------------------------------------------------------

@app.get("/manual")
def manual(page_id: str, db_id: str | None = None):
    """
    Manually trigger the pipeline for a meeting page.

    page_id: the Notion page ID of the meeting recording.
    db_id:   optional — the database ID (with or without dashes) used to look up
             the tenant API key. If omitted, falls back to NOTION_API_KEY env var.
    """
    api_key = None
    if db_id:
        tenant = _TENANTS.get(_normalize_id(db_id))
        if tenant:
            api_key = tenant.get("api_key")
    try:
        return process_meeting(page_id, api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
