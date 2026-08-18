"""
LLM pipeline — mirrors SKILL.md Steps 2 and 3.

extract_metadata()   → infer project, meeting_type, date, attendees grouping, etc.
structure_sections() → parse summary content → AL numbered sections format
"""

import json
import re
from datetime import date

import anthropic

_MODEL = "claude-haiku-4-5-20251001"
_client = anthropic.Anthropic()

def _today_formatted() -> str:
    d = date.today()
    return f"{d.day} {d.strftime('%B')} {d.year}"


_METADATA_SYSTEM = """\
You extract meeting metadata from a Notion AI meeting page. Return ONLY valid JSON — no explanation, no markdown fences.

Output schema:
{
  "project": "Client Co — Meeting Topic",
  "meeting_type": "Discovery Session",
  "date": "08 May 2026",
  "revision": "A",
  "issued_by": "Name",
  "issue_date": "22 June 2026",
  "present": [
    {"practice": "Firm A", "people": ["Alice Smith — Consultant"]},
    {"practice": "Client Co", "people": ["Bob Jones — Director"]}
  ],
  "apologies": [],
  "distribution": "Alice Smith, Bob Jones",
  "next_meeting": "TBD"
}

Rules:
- project: if page lives under a client folder, use "Client — Topic". Otherwise use the meeting topic as project name.
- meeting_type: the descriptive part of the title, stripped of date and "between X and Y". E.g. "Discovery Session", "Weekly Sync", "Fee Proposal Review".
- date: find the meeting date in the content. Format as "D Month YYYY" (e.g. "8 May 2026").
- revision: always "A" for first issue.
- issued_by: the consultant, facilitator, or first-listed attendee. Use their full name.
- issue_date: use today's date exactly as given in the prompt.
- present: group attendees by organisation. Use page title, email domains, meeting context, and role titles to infer orgs. When in doubt, put everyone in one group with "practice": "".
- apologies: anyone listed as absent or sending apologies, or [].
- distribution: comma-separated full names of all present attendees.
- next_meeting: scan action items for next meeting/follow-up call mentions. Extract date if found, else "TBD".
- Return only the JSON object.
"""

_SECTIONS_SYSTEM = """\
You structure meeting summary content into AL numbered sections format. Return ONLY valid JSON — no explanation, no markdown fences.

Output schema:
{
  "sections": [
    {
      "heading": "Section Name",
      "items": [
        {"comment": "The item text.", "action": ""},
        {"comment": "Alice to draft the roadmap by end of month.", "action": "Alice"}
      ]
    }
  ]
}

Rules:
1. Each ### heading → one section entry.
2. Content under each heading → numbered items:
   - Each bullet (- text) → one item.
   - Prose paragraphs → split at sentence boundaries; 1–2 sentences per item. Do not truncate.
   - Every point from the summary must appear somewhere.
3. Action items (- [ ] text): place inside the section where they belong, NOT in a separate "Action Items" section. Set "action" to the owner's first name or short identifier (e.g. "Alice", "Bob", "TBD"). Non-action items get "action": "".
4. A standalone "Action Items" or "Actions" heading: distribute those items into relevant sections, or place in a "Next Steps" section if they have no clear home. Never output a section with heading "Action Items" or "Actions".
5. Strip all footnote citations [^https://...] entirely.
6. Return only the JSON object.
"""


class LLMJSONError(Exception):
    """Raised when the model's JSON output could not be parsed, even after a repair attempt."""


def _extract_json(raw: str) -> str:
    """Strip markdown fences and return the innermost JSON text."""
    text = raw.strip()
    # Handle ```json ... ``` or ``` ... ```
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        return fence.group(1).strip()
    # Fallback: extract outermost { } or [ ]
    for opener, closer in [('{', '}'), ('[', ']')]:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return text[start:end + 1]
    return text


def _parse_json(raw: str) -> dict | list:
    """
    Parse the model's JSON output, self-healing once if it's invalid.

    Meeting content routinely contains quoted phrases (e.g. `referred to as
    "Corridor"`) that models sometimes copy into JSON string values without
    escaping the inner quotes, breaking json.loads. Rather than fail the whole
    pipeline run on that, ask the model to fix its own output once before
    giving up.
    """
    candidate = _extract_json(raw)
    try:
        parsed = json.loads(candidate)
        if not isinstance(parsed, (dict, list)):
            raise LLMJSONError(f"Model JSON parsed but isn't an object/array: {type(parsed).__name__}")
        return parsed
    except json.JSONDecodeError as e:
        print(f"[llm_pipeline] invalid JSON from model ({e}) — attempting repair")
        repair_msg = (
            f"This text was supposed to be valid JSON but failed to parse: {e}\n"
            "Common cause: an unescaped double-quote inside a string value. "
            "Return ONLY the corrected, valid JSON — no explanation, no markdown fences.\n\n"
            f"{candidate}"
        )
        resp = _client.messages.create(
            model=_MODEL,
            max_tokens=8096,
            system="You fix malformed JSON. Return ONLY valid JSON, nothing else.",
            messages=[{"role": "user", "content": repair_msg}],
        )
        repaired = _extract_json(resp.content[0].text.strip())
        try:
            reparsed = json.loads(repaired)
        except json.JSONDecodeError as e2:
            raise LLMJSONError(f"Model JSON invalid even after repair attempt: {e2}") from e2
        if not isinstance(reparsed, (dict, list)):
            raise LLMJSONError(f"Repaired JSON isn't an object/array: {type(reparsed).__name__}")
        return reparsed


def extract_metadata(page_text: str) -> dict:
    """
    Step 2: infer all meeting metadata from the page text.
    Returns a dict matching the SKILL.md metadata schema.
    """
    today = _today_formatted()
    user_msg = f"Today's date (use as issue_date): {today}\n\n{page_text}"

    resp = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=_METADATA_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    return _parse_json(raw)


_MAX_SECTION_INPUT = 6_000  # typical meeting ~3-5k chars; cap protects output token limit


def structure_sections(page_text: str) -> list:
    """
    Step 3: parse summary content into AL numbered sections.
    Returns the 'sections' list matching the SKILL.md schema.
    """
    if len(page_text) > _MAX_SECTION_INPUT:
        page_text = page_text[:_MAX_SECTION_INPUT]

    resp = _client.messages.create(
        model=_MODEL,
        max_tokens=8096,
        system=_SECTIONS_SYSTEM,
        messages=[{"role": "user", "content": page_text}],
    )
    raw = resp.content[0].text.strip()
    result = _parse_json(raw)
    return result.get("sections", result) if isinstance(result, dict) else result
