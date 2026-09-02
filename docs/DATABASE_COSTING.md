# Database & production costing

Prepared 2026-09-02 in response to a question about which database this
app uses in production and roughly what it costs. Pricing pulled from
Google's own current Cloud SQL pricing page (cloud.google.com/sql/pricing,
checked the same day) — treat the totals below as an estimate, not a bill;
the GCP Billing Console is the authoritative source for what you're
actually being charged.

## What database is used

| Environment | Database | Where |
|---|---|---|
| Local dev / demo | SQLite (`tms.db` / `tms_demo.db`) | A file on disk — free, no server |
| Production | **PostgreSQL 16** | Google Cloud SQL, instance `mk-timekeeping-pg` |

The app runs the same SQLAlchemy code against both — `DATABASE_URL` is the
only thing that changes (see `app/db.py`). Production was provisioned by
`deploy_gcp.sh` with these settings:

- **Region:** `asia-south1` (Mumbai)
- **Edition/tier:** Enterprise edition, `db-f1-micro` — Google's smallest
  shared-core tier (0.6 vCPU-equivalent, up to 3 GB storage cap). Explicitly
  right-sized for ~45 internal users, not a default.
- **Storage:** 10 GB SSD, provisioned (see `docs/storage_capacity_estimate.md`
  — actual usage is projected at well under 1 GB even after 5 years, so
  this is deliberate headroom, not a tight fit).
- **High availability:** off (single zone, not a "regional"/HA instance) —
  cheaper, at the cost of a short outage if that one zone has a problem.
  Fine for an internal tool at this scale; revisit if that tradeoff ever
  stops being acceptable.
- **Backups:** Google's automated daily backups (billed separately, small).

Profile photos are NOT in Postgres — they live in a separate Cloud Storage
bucket (`mk-timekeeping-avatars`) mounted into Cloud Run.

## What it costs (current Google list pricing, Mumbai region)

| Item | Rate | Monthly (730 hrs, always-on) |
|---|---|---|
| `db-f1-micro` instance | $0.0105/hour | ~$7.67 |
| 10 GB SSD storage | $0.000232877/GiB-hour | ~$1.70 |
| Automated backups | $0.000109589/GiB-hour of backup data used | a few cents (data volume is tiny — see storage estimate doc) |
| **Cloud SQL subtotal** | | **~$9–10/month** |

One thing worth checking in the GCP Console rather than assuming: Cloud SQL
instances get a public IPv4 address by default unless creation explicitly
disables it, and Google charges **$0.01/hour (~$7.30/month) for an idle
public IP** even if nothing ever connects to it directly (the app itself
connects over a private Unix socket via the Cloud SQL Auth Proxy sidecar,
so it doesn't need that public IP at all). If `mk-timekeeping-pg` still has
one assigned, removing it (`gcloud sql instances patch mk-timekeeping-pg
--no-assign-ip`) would cut roughly $7/month with no functional change.

Two other things run in the same GCP project but aren't the database:

- **Cloud Run** (the app server itself) — pay-per-request/compute-time,
  with a generous free tier (2M requests, 360k GB-seconds/month). At ~45
  users doing normal business-hours usage, this typically stays near or
  fully within the free tier — check Cloud Run's own pricing page for
  current numbers if you want a precise estimate.
- **Cloud Storage** (avatar photos) — a few dozen small images; effectively
  pennies per month.
- **Groq** (the AI day-summary feature, `app/llm_summary.py`) — a separate
  third-party API, not GCP billing, and currently on Groq's free
  developer tier at $0.

## Bottom line

The database itself runs roughly **$9–17/month** depending on whether that
public IP is still provisioned — cheap by design (`db-f1-micro`, no HA,
minimal storage) because ~45 internal users don't need more. The exact
current number is always in **GCP Console → Billing → Reports**, filtered
to the `mk-timekeeping` project; this document is a planning estimate, not
a substitute for checking that.
