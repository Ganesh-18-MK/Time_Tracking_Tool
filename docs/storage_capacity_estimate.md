# Database storage capacity estimate

Prepared 2026-08-01 in response to a team question about how much database
storage this project actually needs, ahead of the GCP Cloud SQL (Postgres)
deployment.

## Logins use zero database storage

Employee sessions are stateless, signed cookies (Starlette's
`SessionMiddleware`) — the session token lives in the browser, not the
database. There is no sessions/logins table in the schema at all. The only
storage tied to authentication is the `password_hash` column on each
`Employee` row (~100 bytes, PBKDF2-SHA256 hash string), set once at
signup. For 45 employees, that's under 5 KB, total, forever.

## What actually drives storage growth

Estimated at 45 employees, ~250 working days/year:

| Table | Rows/year | Size/year (incl. index overhead) |
|---|---|---|
| `task_entries` (time logged) | ~45,000 (45 people × ~4 entries/day) | ~20–25 MB |
| `day_statuses` | ~11,250 | ~2 MB |
| `punch_sessions` + `break_entries` | ~45,000 | ~10 MB |
| `leave_records` | ~675 | <1 MB |
| `audit_log` (admin actions) | ~10,000 | ~4 MB |
| Everything else (roster, config, support, holidays) | fixed, tiny | <1 MB |

**Total: ~40–50 MB/year.**

Profile photos are NOT included above — they live in a separate Cloud
Storage bucket, mounted into Cloud Run, not in Postgres at all (see
`deploy_gcp.sh`).

## Conclusion

Over 5 years of continuous use: roughly 200–250 MB, or ~500 MB–1 GB even
doubling that for safety margin. The 10 GB provisioned in `deploy_gcp.sh`
(`--storage-size=10`) is therefore roughly **10–20x more than 5 years of
realistic usage requires** — deliberate headroom, not a guess.

If usage patterns change significantly (e.g. many more employees, much
higher logging frequency) and this estimate needs revisiting, Cloud SQL
storage can be increased at any time without downtime or migration:

```bash
gcloud sql instances patch mk-timekeeping-pg --storage-size=<new-size-gb>
```
