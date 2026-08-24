"""
Notion API helpers for the Meeting Notes Automation pipeline.

Flow:
  1. find_unprocessed_meetings(db_id, ...) — poll: find pages with empty Status
  2. read_meeting_status(page_id)   — check if page is already Processing/Done
  3. mark_processing(page_id)       — set Status = Processing
  4. fetch_meeting_page(page_id)    — fetch the meeting recording page, return blocks
  5. extract_page_text(page_data)   — render blocks as LLM-readable text
  6. mark_done(page_id, drive_url)  — set Status = Done, write Drive URL
  7. mark_error(page_id, msg)       — set Status = Error, write error message
"""

import os
import re
from datetime import datetime, timezone

from notion_client import Client

_CONTENT_TYPES = {
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "quote", "callout",
}
_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}


def _client() -> Client:
    return Client(auth=os.environ["NOTION_API_KEY"])


def _rt_to_str(rich_text: list) -> str:
    return "".join(r.get("plain_text", "") for r in rich_text).strip()


# ---------------------------------------------------------------------------
# Polling — find candidate pages
# ---------------------------------------------------------------------------

def find_unprocessed_meetings(db_id: str) -> list[dict]:
    """
    Return every page in the Meetings DB with an empty Status, regardless of
    age. Sorted oldest-first so the poll loop processes in creation order.
    Each result carries `id` and `created_time` — enough for the caller to
    apply an age-out without a second fetch.
    """
    notion = _client()
    filter_ = {"property": "Status", "select": {"is_empty": True}}
    sorts = [{"timestamp": "created_time", "direction": "ascending"}]

    results, cursor = [], None
    while True:
        kw = {"database_id": db_id, "filter": filter_, "sorts": sorts}
        if cursor:
            kw["start_cursor"] = cursor
        resp = notion.databases.query(**kw)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]

    return [{"id": p["id"], "created_time": p["created_time"]} for p in results]


# ---------------------------------------------------------------------------
# Meeting page status operations
# ---------------------------------------------------------------------------

def get_parent_database_id(page_id: str) -> str | None:
    """
    Return the (dashless) database ID a page lives in, or None if its parent
    isn't a database. Used to route a bare page_id (from /manual or the
    webhook, where the caller doesn't know which target it belongs to) to
    the right target's Drive folder by matching against targets.json.
    """
    page = _client().pages.retrieve(page_id)
    parent = page.get("parent", {})
    if parent.get("type") != "database_id":
        return None
    return parent["database_id"].replace("-", "")


def read_meeting_status(page_id: str) -> str:
    """Read the Status select value from a meeting page. Returns '' if not set."""
    page = _client().pages.retrieve(page_id)
    props = page.get("properties", {})
    status_prop = props.get("Status", {})
    if status_prop.get("select"):
        return status_prop["select"].get("name", "")
    return ""


def mark_processing(page_id: str):
    _client().pages.update(page_id, properties={
        "Status": {"select": {"name": "Processing"}},
    })


def mark_done(page_id: str, drive_url: str | None):
    props: dict = {"Status": {"select": {"name": "Done"}}}
    if drive_url:
        props["Document"] = {"url": drive_url}
    _client().pages.update(page_id, properties=props)


def mark_error(page_id: str, error_msg: str):
    """Set Status = Error, with the error message in Notes if that property exists.

    Some databases don't have a Notes property. Writing it would make the
    whole update call fail, leaving Status stuck at Processing — so Status is
    always set first, on its own, then Notes is attempted separately.
    """
    client = _client()
    client.pages.update(page_id, properties={"Status": {"select": {"name": "Error"}}})
    try:
        client.pages.update(page_id, properties={
            "Notes": {"rich_text": [{"type": "text", "text": {"content": error_msg[:2000]}}]},
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fetch meeting recording page
# ---------------------------------------------------------------------------

def _blocks_flat(notion: Client, block_id: str, depth: int = 0) -> list:
    """Recursively fetch all child blocks."""
    if depth > 4:
        return []
    blocks = []
    cursor = None
    while True:
        kw = {"block_id": block_id}
        if cursor:
            kw["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kw)
        for block in resp.get("results", []):
            blocks.append(block)
            if block.get("has_children"):
                blocks.extend(_blocks_flat(notion, block["id"], depth + 1))
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return blocks


def _summary_blocks_from_meeting_notes(notion: Client, meeting_notes_block_id: str) -> list:
    """Return only the AI summary child blocks of a meeting_notes block."""
    resp = notion.blocks.children.list(block_id=meeting_notes_block_id)
    children = resp.get("results", [])
    if not children:
        return []
    summary_block = children[0]
    blocks = [summary_block]
    if summary_block.get("has_children"):
        blocks.extend(_blocks_flat(notion, summary_block["id"]))
    return blocks


def _extract_attendees(meeting_notes_block: dict) -> list[str]:
    mn = meeting_notes_block.get("meeting_notes", {})
    attendees = mn.get("meeting_attendees", [])
    names = []
    for a in attendees:
        user = a.get("user", {})
        name = user.get("name", "")
        if name:
            names.append(name)
    return names


def _resolve_user_names(notion: Client, user_ids: list[str]) -> list[str]:
    """Resolve Notion user IDs to display names, falling back to the raw ID
    for any lookup that fails (e.g. a deactivated/guest user)."""
    names = []
    for uid in user_ids:
        try:
            user = notion.users.retrieve(uid)
            names.append(user.get("name") or uid)
        except Exception:
            names.append(uid)
    return names


def fetch_meeting_page(page_id: str) -> dict:
    """
    Fetch a Notion meeting recording page.
    Only the AI-generated summary section is extracted — not notes or transcript.

    Notion's AI meeting-notes block ships as a `transcription` block type
    (not the older `meeting_notes` type this pipeline originally targeted):
    `transcription.status` reports where Notion AI is in generating the
    summary ("notes_ready" once done), and the actual summary content lives
    in a separate block referenced by `transcription.children.summary_block_id`
    — it has to be fetched separately, it's not inline. The `meeting_notes`
    path below is kept as a fallback in case Notion serves that older shape
    for some pages.

    Returns:
        title     str       — page title
        blocks    list      — summary blocks only
        attendees list[str] — attendee names (may be empty)
        ready     bool      — True once the AI summary is actually available
    """
    notion = _client()
    page = notion.pages.retrieve(page_id)

    props = page.get("properties", {})
    title = ""
    for _key, prop in props.items():
        if prop.get("type") == "title":
            for rt in prop.get("title", []):
                title += rt.get("plain_text", "")
            break

    top = notion.blocks.children.list(block_id=page_id)
    all_blocks: list = []
    attendees: list[str] = []
    ready = False

    for block in top.get("results", []):
        btype = block.get("type", "")
        all_blocks.append(block)
        if btype == "transcription":
            transcription = block.get("transcription", {})
            attendee_ids = transcription.get("calendar_event", {}).get("attendees", [])
            if attendee_ids:
                attendees = _resolve_user_names(notion, attendee_ids)
            if transcription.get("status") == "notes_ready":
                summary_block_id = transcription.get("children", {}).get("summary_block_id")
                if summary_block_id:
                    all_blocks.extend(_blocks_flat(notion, summary_block_id))
                    ready = True
        elif btype == "meeting_notes":
            attendees = _extract_attendees(block)
            if block.get("has_children"):
                all_blocks.extend(_summary_blocks_from_meeting_notes(notion, block["id"]))
                ready = True
        elif block.get("has_children"):
            all_blocks.extend(_blocks_flat(notion, block["id"]))

    return {"title": title, "blocks": all_blocks, "attendees": attendees, "ready": ready}


# ---------------------------------------------------------------------------
# Render blocks to LLM-readable text
# ---------------------------------------------------------------------------

def extract_page_text(page_data: dict) -> str:
    lines = []

    title = page_data.get("title", "")
    if title:
        lines.append(f"Page title: {title}")

    attendees = page_data.get("attendees", [])
    if attendees:
        lines.append(f"Attendees: {', '.join(attendees)}")

    lines.append("")
    lines.append("SUMMARY:")

    for block in page_data.get("blocks", []):
        btype = block.get("type", "")

        if btype in _HEADING_TYPES:
            text = _rt_to_str(block.get(btype, {}).get("rich_text", []))
            if text:
                lines.append(f"### {text}")

        elif btype in _CONTENT_TYPES:
            text = _rt_to_str(block.get(btype, {}).get("rich_text", []))
            if text:
                prefix = "- " if btype in ("bulleted_list_item", "numbered_list_item") else ""
                lines.append(f"{prefix}{text}")

        elif btype == "to_do":
            text = _rt_to_str(block.get("to_do", {}).get("rich_text", []))
            if text:
                lines.append(f"- [ ] {text}")

    return "\n".join(lines)
