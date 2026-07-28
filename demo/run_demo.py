"""Run the app against the anonymized demo database on port 8128.

    .venv/bin/python -m demo.run_demo

Safe to show or screen-share anywhere: every name, client, and note in
tms_demo.db is fictional (see demo/make_demo_db.py). The real database and
the real app on port 8127 are untouched.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

demo_db = os.path.join(BASE, "tms_demo.db")
if not os.path.exists(demo_db):
    print("tms_demo.db not found — run:  .venv/bin/python -m demo.make_demo_db")
    sys.exit(1)

# must be set before app.db is imported
os.environ["DATABASE_URL"] = f"sqlite:///{demo_db}"

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8128)
