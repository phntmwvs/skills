---
name: job-brief
description: "Recurring job-search brief for any seeker: profile JSON drives sources, filters, and email."
version: 0.1.0
author: oxy
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [cron, email, jobs, job-search]
    related_skills: [himalaya]
---

# Job Brief

Generic daily job-search digest, usable by anyone. A single profile JSON defines who the seeker is, what to search for, where to search, and who gets the email. The bundled script gathers listings, filters them, scores them, and emails a top-N brief via `himalaya`. Multiple seekers coexist — one profile file and one cron job each.

## When to Use

- "Send me a daily email of remote jobs matching X."
- "Set up a job-search brief for my friend/partner/client."
- A cron tick fires for an existing profile (jump to Procedure — Tick).

Don't use for: one-off "what jobs are out there" lookups (use `web_search` directly), or resume tailoring per posting.

## Prerequisites

- `himalaya` CLI configured with a sending account (load the `himalaya` skill; the profile's `email_from` must match a configured account name). **himalaya v2 requires an explicit `From:` header** — the script reads it from `JOB_BRIEF_FROM` in `~/.hermes/.env` and adds it automatically; set that variable to the account's address.
- API keys in `~/.hermes/.env` depending on enabled sources:
  - `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` (free at https://developer.adzuna.com) — required for the `adzuna` source.
  - `SERPAPI_KEY` — required for the `serpapi` source.
  - Remotive, RemoteOK, Jobicy need no keys.
- Script: `scripts/job-brief.py` in this skill's directory. Profile template: `templates/profile.example.json`.

## Procedure — Setup (once per seeker)

### 1. Build the profile

Copy `templates/profile.example.json` to a per-seeker file (e.g. `~/.hermes/job-brief/<name>.json`) and edit:

| Field | Meaning |
|-------|---------|
| `seeker.name` / `seeker.notes` | Name on the brief; context line printed at the top |
| `recipients` | Email addresses that receive the brief |
| `email_from` | himalaya account name to send from |
| `min_salary_usd` | Drop listings whose salary (or salary max) is below this; `0` disables |
| `max_jobs` | How many to include (default 10) |
| `queries` | `[["what", "where"], ...]` pairs for Adzuna/SerpApi. Embed `remote` in the *what* — Adzuna's `where=remote` is broken (returns city jobs) |
| `strong_title_keywords` | Title hits get a big score boost |
| `relevant_title_keywords` | Hard relevance gate: a per-query hit must match one of these (or a `tool_keywords` hit in the description). Bulk sources are pre-filtered by this list |
| `tool_keywords` | Tool/product names that signal relevance (e.g. `"salesforce"`, `"guidewire"`) |
| `exclude_title_keywords` | Hard drop on title (e.g. `"senior"`, `"director"`, `"engineer"`) |
| `exclude_location_keywords` | Hard drop — matched against title, location, **and** description (catches "EMEA" mentioned anywhere) |
| `allowed_locations` | Local cities/states that pass the gate alongside remote |
| `remote_only` | `true`: only remote or `allowed_locations` pass. `false`: no location gate |
| `sources` | Toggle `adzuna`, `serpapi`, `remotive`, `remoteok`, `jobicy` |
| `state_dir` | Where seen-jobs DBs live (default `~/.hermes/jobs`) |

Done when the seeker (or you) can answer "would this posting pass?" deterministically for a few synthetic examples.

### 2. Dry-run in the foreground

`terminal(command="python3 ~/.hermes/skills/productivity/job-brief/scripts/job-brief.py <profile.json> --dry-run", timeout=300)`

The dry run prints the brief plus a per-source count, exclusion tallies, and the flagged-scam list — no email, but it *does* mark listings seen (a later real run won't re-show them). Iterate on keywords until the brief looks right: if every result is irrelevant, tighten `relevant_title_keywords`; if too few, loosen it or add queries.

Done when one dry run returns a brief the seeker would open.

### 3. Test email, then schedule

`python3 .../job-brief.py <profile.json> --test` sends a one-line probe to all recipients. When it lands, create the cron job:

```
cronjob_manage(action="create",
               schedule="0 7 * * *",
               prompt="Run: python3 ~/.hermes/skills/productivity/job-brief/scripts/job-brief.py <profile.json> — report failures only.",
               deliver=<user's destination>)
```

Done when the job exists and the probe email arrived.

## Procedure — Tick (each scheduled run)

Run the script without flags: `python3 .../job-brief.py <profile.json>`. It searches all enabled sources, dedupes against the per-profile seen DB (and by title+company within the run), applies exclude/location/relevance gates, scam-flags, enforces the salary floor, scores, and emails the top `max_jobs`. Listings are only marked seen **after** a successful send — a failed email means the next run retries them. Report failures; stay silent on success unless the brief is empty several days running (then suggest loosening keywords).

## Pitfalls

- **Adzuna `where=remote` is broken** — it returns US city jobs, not remote ones. Put `remote` in the query text instead; the location gate does the real filtering.
- **Dry runs consume seen-state.** Re-running a dry run shows only *new* listings. To re-test from scratch, delete `~/.hermes/jobs/seen-jobs-<profile-slug>.json`.
- **`email_from` must be a himalaya account name**, not an address. Sending uses `himalaya message send -a <account>`; himalaya v2 rejects messages without a `From:` header, so the script injects one from `JOB_BRIEF_FROM` in `~/.hermes/.env` — set it to the account's address or sends fail with "No `From:` header found in raw message".
- **Salary parsing is heuristic** — it reads `$X–$Y`, `$X/yr`, `$X/hr` (annualized at 2080 h) from free text. Listings with no parseable salary pass the floor check (only *known-low* salaries are dropped).
- **Bulk sources ignore `queries`** — Remotive/RemoteOK/Jobicy fetch their full feed and filter by `relevant_title_keywords` only, so keep that list tight or the brief floods.
- **`%-d` in date formatting is POSIX-only** — on Linux/macOS fine; don't run on Windows.

## Verification

- [ ] Profile JSON parses and has `recipients` + `queries` (script exits 2 otherwise).
- [ ] One `--dry-run` produced a plausible brief with per-source counts in stderr.
- [ ] `--test` email arrived at every recipient.
- [ ] Cron job exists; its first real run logged `brief sent: N jobs` in stderr.
- [ ] Seen DB exists at `~/.hermes/jobs/seen-jobs-<slug>.json` after a run.
