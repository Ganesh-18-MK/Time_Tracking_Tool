"""AI day summary — calls Groq's hosted API to turn one employee's day of
TaskEntry rows into a short prose summary, shown only on the admin Task Logs
report (see reports.daily_task_log_report()).

This REPLACES the self-hosted-Ollama version built 2026-09-02, itself a
replacement for the Gemini version from 2026-08-31 — the fifth backend this
one feature has had counting the original Anthropic call and the rule-based-
only interlude (see git history / CLAUDE.md for the full chain). Ganesh's
explicit requirement this time (2026-09-02): "i dont want to spend any
amount on this, i want it to be free." Ollama itself is free, but making it
reachable from the deployed Cloud Run app reliably needs an always-on host
somewhere, which costs real money (a small GCP VM runs ~$25-50/mo) — running
it only on a personal machine isn't reliable for production. Groq removes
that infrastructure problem entirely: it's a hosted API (so no VM to run or
pay for) with a genuinely free, no-credit-card developer tier.

Data-handling tradeoff, checked via web search before committing to this
(2026-09-02, since this is exactly the kind of compliance question that
made Ganesh move off Gemini's free tier in the first place): Groq states it
does not use customer prompts/responses to train or fine-tune any model,
and this policy is account-wide — NOT split between free and paid tiers,
unlike Gemini's split. Groq's default prompt retention is 30 days, with a
self-serve Zero Data Retention option available in their dashboard if
Ganesh wants prompts discarded immediately instead. This is a real
improvement over the Gemini free-tier situation, but it is still a
third-party API — unlike the Ollama version this replaces, task details and
client names DO leave this app's own infrastructure and reach Groq's
servers, just under a no-training guarantee rather than under Gemini's
training-permitted "Unpaid Services" terms. If that distinction ever stops
being good enough, self-hosted Ollama is the fallback with zero data
leaving Ganesh's own infrastructure at all, at the cost of needing to pay
for and run a host.

Talks to Groq's OpenAI-compatible `/openai/v1/chat/completions` endpoint —
same request/response shape this module's Ollama version already used, so
the swap is almost entirely a base-URL/auth-header change plus new env var
names; `_build_prompt()` is unchanged for the fourth time in a row.

Same "never raises" contract as every version before it: a missing API key,
a network error, a timeout, a non-200 response, or an unexpected response
shape all come back as (None, "<short reason>") instead of an exception, so
a Groq outage can never break Submit Day (see submit_day() in
app/routes/employee.py, the only caller) or the Task Logs report page.
"""
import os
from typing import List, Optional, Tuple

import httpx

# Model, not just endpoint, is configurable via env var — same "don't
# hardcode what might need tuning" instinct as this app's own Config table
# elsewhere. Ganesh's original pick (2026-09-02) was llama-3.1-8b-instant,
# but Groq deprecated that model 06/17/26 with a shutdown date of 08/16/26 —
# already past by the time this was actually tested end-to-end (2026-09-02),
# so every request came back "Groq returned HTTP 404" (see Task Logs report
# screenshot the same day). Switched to openai/gpt-oss-20b, Groq's own
# documented replacement for llama-3.1-8b-instant
# (console.groq.com/docs/deprecations, checked 2026-09-02) — comparable
# speed/size for this short, low-stakes summarization task.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Groq's hardware is fast, so this can stay short like the original Gemini
# version rather than the longer allowance the self-hosted-CPU Ollama
# version needed. Still sits in the middle of Submit Day's request/response
# cycle (no background job queue in this app), so a hung request must never
# turn into a hung Submit Day click for the employee waiting on it.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "10"))

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
        "Summarize this person's workday for their manager as 3 to 5 short bullet points. "
        "Each bullet must start with a hyphen (-) on its own line. Name the specific "
        "projects/clients in each bullet. Be factual and concise — no greetings, no filler, "
        "no headers, no closing remarks, just the bullet lines themselves.\n\n"
        f"Task log:\n{body}"
    )


def summarize_day(entries: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    """`entries`: [{"project": str, "task": str, "duration_minutes": int,
    "details": str}, ...] for one employee's one day — already resolved to
    plain strings by the caller (submit_day()) rather than passed as ORM
    rows, so this module has zero DB/model coupling and can be unit tested
    or swapped out on its own (this is the third time it's been swapped —
    see this module's own docstring).

    Returns (summary_text, None) on success, or (None, reason) on any
    failure — see this module's own docstring for the full list of
    swallowed failure modes. Never raises."""
    if not entries:
        return None, "no entries to summarize"

    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY not set"

    prompt = _build_prompt(entries)
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        # openai/gpt-oss-20b is a reasoning model (unlike llama-3.1-8b-instant,
        # its predecessor here) — it spends part of its token budget on a
        # hidden chain-of-thought, returned separately in a `reasoning` field,
        # before writing the actual answer into `content`. The original
        # max_tokens: 220 budget (fine for a plain chat model) let it burn
        # every token on that reasoning step and never reach a final answer,
        # so `content` came back "" — surfaced as "Groq returned an empty
        # summary" (Ganesh, 2026-09-02, caught live the same way the 404 was).
        # reasoning_effort: "low" keeps the hidden thinking step short for
        # this low-stakes summarization task (Groq's own documented knob for
        # this — console.groq.com/docs/reasoning), and the raised budget
        # leaves real room for the answer even if some reasoning still
        # happens. Only GPT-OSS models support reasoning_effort low/med/high;
        # harmless to send even if GROQ_MODEL is later pointed at a
        # non-reasoning model that ignores unknown fields.
        "max_completion_tokens": 500,
        "reasoning_effort": "low",
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    try:
        resp = httpx.post(GROQ_API_BASE, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        return None, "Groq request timed out"
    except httpx.HTTPError as e:
        return None, f"Groq request failed: {e}"

    if resp.status_code != 200:
        # Never echo the raw response body into stored data — it can
        # include the request payload (i.e. the employee's own task
        # details) reflected back in an error message on a 4xx. A short,
        # generic, status-coded reason is enough for an admin to know a
        # summary is missing and why, without re-storing the exact content
        # this whole feature is already sensitive about.
        return None, f"Groq returned HTTP {resp.status_code}"

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError):
        return None, "Groq response had an unexpected shape"

    text = text.strip()
    if not text:
        return None, "Groq returned an empty summary"
    return text, None
