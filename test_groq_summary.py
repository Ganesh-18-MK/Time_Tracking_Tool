"""Ad-hoc local test for the Groq AI day-summary backend (app/llm_summary.py).
Not part of the test suite — just a quick way to eyeball the prompt/output
without spinning up the app, the DB, or going through Submit Day.

Run:
    export GROQ_API_KEY=your_key_here
    .venv/bin/python test_groq_summary.py

Edit the `entries` list below to try different task logs. Safe to delete
once you're done checking the output.
"""
from app.llm_summary import _build_prompt, summarize_day

entries = [
    # Logged FIRST, but least time — should rank LAST in the summary now.
    {"project": "Northwind Consulting Inc.", "task": "Reply to email",
     "duration_minutes": 45, "details": "Responded to client questions about I-140 timeline"},
    {"project": "Internal", "task": "Team meeting",
     "duration_minutes": 30, "details": ""},
    # Logged LAST, but most total time (90+60=150min=2:30) — should rank
    # FIRST, since the 2026-09-02 quantify-it fix sorts by time, not
    # chronological order. If the printed prompt/result below leads with
    # Bluepeak despite it being listed last here, the fix is working.
    {"project": "Bluepeak Consulting Inc.", "task": "Check credential and post job order",
     "duration_minutes": 90, "details": "Reviewed H-1B credentials, posted job order to state portal"},
    {"project": "Bluepeak Consulting Inc.", "task": "Get AD quotes",
     "duration_minutes": 60, "details": "Requested prevailing wage quotes from 3 attorneys"},
]

print("=" * 70)
print("PROMPT SENT TO GROQ:")
print("=" * 70)
print(_build_prompt(entries))
print()

text, error = summarize_day(entries)

print("=" * 70)
print("RESULT:")
print("=" * 70)
if error:
    print(f"ERROR: {error}")
else:
    print(text)
