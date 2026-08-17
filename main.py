import asyncio
import hashlib
import hmac
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from drive import upload_to_drive
from llm_pipeline import extract_metadata, structure_sections
from notion_utils import (
    extract_page_text,
    fetch_meeting_page,
    find_unprocessed_meetings,
    mark_done,
    mark_error,
    mark_processing,
    read_meeting_status,
)
from scripts.generate_docx import generate_docx


class MeetingNotReadyError(Exception):
    """Notion AI hasn't finished generating the summary yet — safe for Notion to retry."""


def _yy_mm_dd(date_str: str) -> str:
    """Reverse-order date (yy-mm-dd) for sortable filenames, per AL's naming convention."""
    try:
        return datetime.strptime(date_str, "%d %B %Y").strftime("%y-%m-%d")
    except (ValueError, TypeError):
        return date_str.replace(" ", "-") or "unknown-date"


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("AL Meeting Notes service starting...")
    poll_task = asyncio.create_task(_poll_loop())
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="AL Meeting Notes Automation", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "al-meeting-notes"}


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def claim_meeting(page_id: str) -> str | None:
    """
    Fast, synchronous portion of the pipeline — cheap enough to run inline
    before responding to Notion's webhook, and what the poll loop calls once
    per candidate page each cycle:

    1. Check status — skip if already Done or Processing.
    2. Fetch the Notion AI meeting recording page.
    3. Check readiness — Notion AI may still be writing the summary.
    4. Only now mark as Processing (nothing written yet if not ready).
    5. Render blocks to LLM-readable text.

    Returns the page text if the meeting is ready to process, or None if it
    was already Done/Processing and should be skipped. Raises
    MeetingNotReadyError if Notion AI hasn't finished writing the summary yet
    — deliberately NOT caught below, so a not-ready page is left with an
    empty Status (not Error) and retried next cycle instead of being
    permanently marked failed the moment the poller sees it.
    """
    status = read_meeting_status(page_id)
    if status in ("Done", "Processing"):
        return None

    try:
        page_data = fetch_meeting_page(page_id)

        if not page_data["blocks"]:
            raise MeetingNotReadyError("Meeting page has no summary blocks — Notion AI may still be processing.")

        mark_processing(page_id)

        return extract_page_text(page_data)

    except MeetingNotReadyError:
        raise
    except Exception as e:
        error_msg = str(e)
        print(f"[pipeline] Error claiming {page_id}: {error_msg}")
        try:
            mark_error(page_id, error_msg)
        except Exception:
            pass
        raise


def run_pipeline(page_id: str, page_text: str) -> dict:
    """
    Slow portion of the pipeline — LLM calls, DOCX generation, Drive upload.
    Runs after Notion has already been ack'd, so it isn't subject to the
    webhook delivery timeout:

    5. Extract metadata (LLM).
    6. Structure sections (LLM).
    7. Assemble final JSON.
    8. Generate DOCX from template.
    9. Upload to Google Drive.
    10. Mark as Done with Drive URL.
    """
    try:
        metadata = extract_metadata(page_text)
        sections = structure_sections(page_text)

        doc_data = {**metadata, "sections": sections}

        meeting_name = re.sub(r"[^\w\s-]", "", metadata.get("project") or "Meeting").strip()[:60]
        date_slug = _yy_mm_dd(metadata.get("date") or "")
        filename = f"{date_slug}_{meeting_name}_AL Notes.docx"

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = tmp.name

        generate_docx(doc_data, Path(tmp_path))
        drive_url = upload_to_drive(tmp_path, filename)

        mark_done(page_id, drive_url)

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
            mark_error(page_id, error_msg)
        except Exception:
            pass
        raise


def process_meeting(page_id: str) -> dict:
    """Full pipeline, run synchronously start to finish. Used by /manual."""
    page_text = claim_meeting(page_id)
    if page_text is None:
        return {"status": "skipped", "reason": "page is already Done or Processing"}
    return run_pipeline(page_id, page_text)


def process_meeting_background(page_id: str, page_text: str) -> None:
    """Fire-and-forget wrapper for BackgroundTasks — errors are already
    recorded on the page by run_pipeline, nothing left to propagate to."""
    try:
        run_pipeline(page_id, page_text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Polling — replaces the Notion webhook as the trigger source. al-vps has no
# public ingress, so the service calls out to Notion on a timer instead of
# waiting to be called in. See projects/OS/al-vps/al-meeting-notes-migration-
# PLAN.md in MOLIOR-OS for why (Notion's webhook sender 502s on the .ts.net
# Funnel domain regardless of port — ruled out exhaustively, not fixable
# within Tailscale Funnel).
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
_POLL_LOOKBACK_HOURS = float(os.environ.get("POLL_LOOKBACK_HOURS", "48"))
_POLL_AGE_OUT_HOURS = float(os.environ.get("POLL_AGE_OUT_HOURS", "6"))
_MEETINGS_DB_ID = os.environ.get("NOTION_MEETINGS_DB_ID", "")


def _parse_notion_timestamp(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _poll_once() -> int:
    """
    One synchronous poll cycle — find candidate pages, process each in turn.
    Runs entirely on a worker thread (see _poll_loop); safe to make blocking
    Notion/Claude/Drive calls here.

    Sequential by design: only one page is ever in flight at a time, so no
    distributed lock is needed to avoid double-processing a page.

    Returns the number of candidate pages seen, for the heartbeat log.
    """
    candidates = find_unprocessed_meetings(_MEETINGS_DB_ID, _POLL_LOOKBACK_HOURS)

    for page in candidates:
        page_id = page["id"]
        try:
            page_text = claim_meeting(page_id)
        except MeetingNotReadyError as e:
            age_hours = (
                datetime.now(timezone.utc) - _parse_notion_timestamp(page["created_time"])
            ).total_seconds() / 3600
            if age_hours >= _POLL_AGE_OUT_HOURS:
                print(f"[poll] {page_id} aged out after {age_hours:.1f}h, not ready: {e}")
                try:
                    mark_error(page_id, f"Gave up after {_POLL_AGE_OUT_HOURS}h waiting for Notion AI summary: {e}")
                except Exception:
                    pass
            else:
                print(f"[poll] {page_id} not ready yet ({age_hours:.1f}h old): {e}")
            continue
        except Exception as e:
            # claim_meeting already wrote Status=Error for non-readiness
            # failures; just log and move on to the next candidate.
            print(f"[poll] error claiming {page_id}: {e}")
            continue

        if page_text is None:
            continue  # already Done/Processing by the time we got to it

        try:
            run_pipeline(page_id, page_text)
            print(f"[poll] processed {page_id}")
        except Exception as e:
            # run_pipeline already wrote Status=Error; just log and continue.
            print(f"[poll] error processing {page_id}: {e}")

    return len(candidates)


async def _poll_loop() -> None:
    if not _MEETINGS_DB_ID:
        print("[poll] NOTION_MEETINGS_DB_ID not set — polling disabled")
        return

    print(f"[poll] starting: interval={_POLL_INTERVAL_SECONDS}s lookback={_POLL_LOOKBACK_HOURS}h "
          f"age_out={_POLL_AGE_OUT_HOURS}h db={_MEETINGS_DB_ID}")

    while True:
        try:
            count = await asyncio.to_thread(_poll_once)
            print(f"[poll] cycle complete — {count} candidate(s)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let the loop die — a Notion outage or transient error
            # this cycle just means we try again next cycle.
            print(f"[poll] cycle failed: {e}")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


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


def _verify_notion_signature(raw_body: bytes, header_sig: str | None) -> bool:
    """
    Verify Notion's per-event webhook signature (sent as `X-Notion-Signature:
    sha256=<hex>`, HMAC-SHA256 over the raw request body keyed by the
    `verification_token` captured during the one-time subscription handshake
    — see https://developers.notion.com/reference/webhooks). Only applies to
    the "Notion API integration webhook subscription" delivery path; Notion's
    legacy database Automation "Send a webhook" action has no signing
    mechanism at all, so that path stays unauthenticated by Notion's own
    design (defense against it is network-level: this endpoint's URL is not
    published anywhere, and this is the only public path on the host).
    """
    secret = os.environ.get("NOTION_WEBHOOK_SECRET")
    if not secret:
        return True
    if not header_sig:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)


@app.get("/webhook/notion")
async def webhook_notion_verify():
    return {"status": "ok"}


def _extract_page_id(body: dict) -> str | None:
    """Return the page id from either the automation payload or the
    integration-webhook event payload."""
    event_type = body.get("type")
    if event_type is not None:
        if event_type not in _SUBSCRIBED_EVENT_TYPES:
            return None
        entity = body.get("entity") or {}
        if entity.get("type") != "page":
            return None
        return entity.get("id")

    # Legacy database-automation payload
    return body.get("data", {}).get("id")


@app.post("/webhook/notion")
async def webhook_notion(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    body = json.loads(raw_body)
    print(f"[webhook/notion] payload: {body}")

    if "verification_token" in body:
        # One-time handshake, unsigned by definition (there's no secret yet
        # to sign with — this request is what MINTS the secret). Always
        # trusted regardless of NOTION_WEBHOOK_SECRET; harmless (no data
        # access) and this is the only way Notion re-verifies a subscription.
        print(f"[webhook/notion] verification_token: {body['verification_token']}")
        return JSONResponse({"status": "ok"})

    if not _verify_notion_signature(raw_body, request.headers.get("x-notion-signature")):
        print("[webhook/notion] rejected: invalid or missing X-Notion-Signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    page_id = _extract_page_id(body)

    if not page_id:
        return JSONResponse({"status": "skipped", "reason": "no page id in payload"})

    try:
        page_text = claim_meeting(page_id)
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

    if page_text is None:
        return JSONResponse({"status": "skipped", "reason": "page is already Done or Processing"})

    # Ack Notion immediately — the LLM/DOCX/Drive work runs after the response
    # goes out, so it's no longer subject to Notion's webhook delivery timeout.
    background_tasks.add_task(process_meeting_background, page_id, page_text)
    return JSONResponse({"status": "accepted", "page_id": page_id})


# ---------------------------------------------------------------------------
# Manual trigger — for testing or missed webhook events
# ---------------------------------------------------------------------------

@app.get("/manual")
def manual(page_id: str):
    """Manually trigger the pipeline for a meeting page."""
    try:
        return process_meeting(page_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
