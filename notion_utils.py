"""
Notion API helpers for the Meeting Notes Automation pipeline.

Flow:
  1. read_meeting_status(page_id)   — check if page is already Processing/Done
  2. mark_processing(page_id)       — set Status = Processing
  3. fetch_meeting_page(page_id)    — fetch the meeting recording page, return blocks
  4. extract_page_text(page_data)   — render blocks as LLM-readable text
  5. mark_done(page_id, drive_url)  — set Status = Done, write Drive URL
  6. mark_error(page_id, msg)       — set Status = Error, write error message
"""

import os
import re

from notion_client import Client

_CONTENT_TYPES = {
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "quote", "callout",
}
_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}


def _client(api_key: str | None = None) -> Client:
    return Client(auth=api_key or os.environ["NOTION_API_KEY"])


def _rt_to_str(rich_text: list) -> str:
    return "".join(r.get("plain_text", "") for r in rich_text).strip()


# ---------------------------------------------------------------------------
# Meeting page status operations
# ---------------------------------------------------------------------------

def get_page_database_id(page_id: str, api_key: str | None = None) -> str:
    """Return the (dash-containing) database_id that a page currently lives under, or ''."""
    page = _client(api_key).pages.retrieve(page_id)
    parent = page.get("parent", {})
    return parent.get("database_id", "") or ""


def read_meeting_status(page_id: str, api_key: str | None = None) -> str:
    """Read the Status select value from a meeting page. Returns '' if not set."""
    page = _client(api_key).pages.retrieve(page_id)
    props = page.get("properties", {})
    status_prop = props.get("Status", {})
    if status_prop.get("select"):
        return status_prop["select"].get("name", "")
    return ""


def mark_processing(page_id: str, api_key: str | None = None):
    _client(api_key).pages.update(page_id, properties={
        "Status": {"select": {"name": "Processing"}},
    })


def mark_done(page_id: str, drive_url: str | None, api_key: str | None = None):
    props: dict = {"Status": {"select": {"name": "Done"}}}
    if drive_url:
        props["Document"] = {"url": drive_url}
    _client(api_key).pages.update(page_id, properties=props)


def mark_error(page_id: str, error_msg: str, api_key: str | None = None):
    """Set Status = Error, with the error message in Notes if that property exists.

    Some tenant databases don't have a Notes property. Writing it would make the
    whole update call fail, leaving Status stuck at Processing — so Status is
    always set first, on its own, then Notes is attempted separately.
    """
    client = _client(api_key)
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


def fetch_meeting_page(page_id: str, api_key: str | None = None) -> dict:
    """
    Fetch a Notion meeting recording page.
    Only the AI-generated summary section is extracted — not notes or transcript.

    Returns:
        title     str       — page title
        blocks    list      — summary blocks only
        attendees list[str] — names from the meeting_notes attendee list (may be empty)
    """
    notion = _client(api_key)
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

    for block in top.get("results", []):
        btype = block.get("type", "")
        all_blocks.append(block)
        if btype == "meeting_notes":
            attendees = _extract_attendees(block)
            if block.get("has_children"):
                all_blocks.extend(_summary_blocks_from_meeting_notes(notion, block["id"]))
        elif block.get("has_children"):
            all_blocks.extend(_blocks_flat(notion, block["id"]))

    return {"title": title, "blocks": all_blocks, "attendees": attendees}


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
