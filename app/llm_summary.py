"""AI day summary (Ganesh, 2026-08-31) — calls Google's Gemini API to turn
one employee's day of TaskEntry rows into a short prose summary, shown only
on the admin Task Logs report (see reports.daily_task_log_report()).

This REPLACES, for any day where it succeeds, the deterministic
rule_based_day_summary() the app switched to on 2026-08-29 (Ganesh: "without
LLM cant we generate summary" -> "Replace LLM with rule-based" — the
original LLM path, app/llm.py, was deleted that day as fully unreferenced).
Bringing an LLM call back in is a deliberate reversal of that decision, not
an oversight — see the CLAUDE.md bullet for this feature for the reasoning
and the tradeoffs Ganesh chose to accept.

Known, accepted risk (confirmed with Ganesh, 2026-08-31, after checking
Google's current Gemini API Additional Terms of Service): a plain API key
with no Cloud Billing account attached runs against Google's "Unpaid
Services" tier, whose own terms say prompts/responses ARE used to improve
Google's products, and explicitly instruct "do not submit sensitive,
confidential, or personal information" to that tier. This app's task notes
can include client names (Case Type projects — see Project.is_case_type),
so a free-tier key is a real compliance exposure for a law firm, not just a
privacy nicety. Ganesh chose to start on the free tier anyway ("initially i
want to go with free gemini api key, i am already had a key") with intent
to move to a billed project later — the SAME API key works under both
tiers unchanged (billing is a property of the linked Google Cloud project,
not the key), so upgrading later needs no code change here, just enabling
billing on that project. See README.md's "AI day summaries" section for
the full writeup — don't remove that warning if this file is ever touched
again without re-confirming the tier decision.

Mirrors the shape of the original (deleted) app/llm.py: summarize_day()
never raises — a missing key, a network error, a timeout, a non-200
response, or an unexpected response shape all come back as
(None, "<short reason>") instead of an exception, so a Gemini outage can
never break Submit Day (see submit_day() in app/routes/employee.py, the
only caller) or the Task Logs report page.
"""
import os
from typing import List, Optional, Tuple

import httpx

# Model, not tier, is what's configurable here — the "flash" line is
# Google's own cost/latency-optimized tier for exactly this kind of short,
# low-stakes text task. Overridable via env var (not hardcoded) so a model
# ID Google later deprecates can be swapped on the host without a redeploy,
# same "don't hardcode what might need tuning" instinct as this app's own
# Config table elsewhere — this one's a plain env var rather than a Config
# row since it's a deploy-time technical knob, not a business threshold an
# admin should browse.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Short and deliberately unforgiving — this call sits in the middle of
# Submit Day's request/response cycle (no background job queue in this app,
# see the "no background scheduler" note elsewhere in this codebase), so a
# hung Gemini request must never turn into a hung Submit Day click for the
# employee waiting on it.
REQUEST_TIMEOUT_SECONDS = 10.0

MAX_ENTRIES_IN_PROMPT = 40  # a real day almost never has more than this; caps prompt size/cost regardless


def _build_prompt(entries: List[dict]) -> str:
    lines = []
    for e in entries[:MAX_ENTRIES_IN_PROMPT]:
        details = (e.get("details") or "").strip()
        line = f"- {e['project']} / {e['task']} ({e['duration_minutes']} min)"
        if details:
            line += f": {details}"
        lines.append(line)
    body = "\n".join(lines)
    return (
        "Summarize this person's workday in 3-4 short sentences for their manager. "
        "Name the specific projects/clients they worked on. Be factual and concise — "
        "no greetings, no filler, no markdown formatting, plain prose only.\n\n"
        f"Task log:\n{body}"
    )


def summarize_day(entries: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    """`entries`: [{"project": str, "task": str, "duration_minutes": int,
    "details": str}, ...] for one employee's one day — already resolved to
    plain strings by the caller (submit_day()) rather than passed as ORM
    rows, so this module has zero DB/model coupling and can be unit tested
    or swapped out on its own.

    Returns (summary_text, None) on success, or (None, reason) on any
    failure — see this module's own docstring for the full list of
    swallowed failure modes. Never raises."""
    if not entries:
        return None, "no entries to summarize"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not set"

    prompt = _build_prompt(entries)
    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 220},
    }

    try:
        resp = httpx.post(
            url, params={"key": api_key}, json=payload, timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        return None, "Gemini request timed out"
    except httpx.HTTPError as e:
        return None, f"Gemini request failed: {e}"

    if resp.status_code != 200:
        # Never echo the raw response body into stored data — it can
        # include the request payload (i.e. the employee's own task
        # details) reflected back in an error message on a 4xx. A short,
        # generic, status-coded reason is enough for an admin to know a
        # summary is missing and why, without re-storing the exact content
        # this whole feature is already sensitive about.
        return None, f"Gemini returned HTTP {resp.status_code}"

    try:
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError, TypeError):
        return None, "Gemini response had an unexpected shape"

    text = text.strip()
    if not text:
        return None, "Gemini returned an empty summary"
    return text, None
