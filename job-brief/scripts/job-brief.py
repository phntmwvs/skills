#!/usr/bin/env python3
"""
Generic daily job-search brief, driven by a profile JSON.

Sources (all optional, toggle in profile):
  - Adzuna API      (needs ADZUNA_APP_ID + ADZUNA_APP_KEY in ~/.hermes/.env)
  - SerpApi / Google for Jobs (needs SERPAPI_KEY)
  - Remotive        (free, no key; client-side title filter)
  - RemoteOK        (free, no key; client-side title filter)
  - Jobicy          (free, no key; client-side title filter)

Pipeline: gather -> dedupe -> hard-exclude -> relevance gate -> location gate
-> scam flag -> salary floor -> score -> top-N -> email via himalaya.

Usage:
  python3 job-brief.py <profile.json>            # search + email brief
  python3 job-brief.py <profile.json> --dry-run  # print brief + debug, no email
  python3 job-brief.py <profile.json> --test     # send test email only
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ENV_PATH = Path.home() / ".hermes" / ".env"
HIMALAYA_CANDIDATES = [
    "/opt/homebrew/bin/himalaya",
    "/usr/local/bin/himalaya",
    "himalaya",  # fall back to PATH
]

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

SALARY_PATTERNS = [
    (re.compile(r"\$([\d,]{5,7})\s*[-–—to]{1,3}\s*\$([\d,]{5,7})", re.I), "range"),
    (re.compile(r"\$([\d,]{5,7})", re.I), "single"),
    (re.compile(r"\$([\d,]{2,3})\s*[-–—to]{1,3}\s*\$([\d,]{2,3})(?:\s*/|\s*per)?\s*(?:hr|hour)", re.I), "hourly-range"),
    (re.compile(r"\$([\d,]{2,3})\s*/?\s*(?:hr|hour)", re.I), "hourly"),
]
SCAM_PATTERNS = [
    (re.compile(r"bank(ing)?\s+(account|details)", re.I), "asks for banking details"),
    (re.compile(r"social\s+security", re.I), "mentions social security number"),
    (re.compile(r"photo\s*id|driver'?s?\s*license|passport\s+copy", re.I), "asks for photo ID"),
    (re.compile(r"\b(?:bitcoin|crypto(currency)?|usdt|btc|eth\b)", re.I), "mentions cryptocurrency"),
    (re.compile(r"purchase.{0,40}(?:required|necessary)|required\s+purchase|equipment\s+fee", re.I), "required purchase / equipment fee"),
]
STRONG_REMOTE_PHRASES = [
    "fully remote", "100% remote", "remote-first", "remote first",
    "work from home", "work from anywhere", "remote position",
    "remote role", "remote opportunity", "remote job",
]
HYBRID_MARKERS = [
    "hybrid", "on-site", "onsite", "in-office", "in office",
    "office-based", "relocation",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

DEFAULTS = {
    "recipients": [],
    "email_from": "jobs",
    "subject_prefix": "Job brief",
    "min_salary_usd": 0,
    "max_jobs": 10,
    "queries": [],
    "strong_title_keywords": [],
    "relevant_title_keywords": [],
    "tool_keywords": [],
    "exclude_title_keywords": [],
    "exclude_location_keywords": [],
    "allowed_locations": [],
    "remote_only": True,
    "sources": {"adzuna": True, "serpapi": False, "remotive": True,
                "remoteok": True, "jobicy": True},
    "state_dir": "~/.hermes/jobs",
    "seeker": {"name": "", "notes": ""},
}


def load_profile(path: str) -> dict:
    p = dict(DEFAULTS)
    user = json.loads(Path(path).read_text())
    for k, v in user.items():
        if k == "sources":
            src = dict(DEFAULTS["sources"])
            src.update(v or {})
            p["sources"] = src
        elif k == "seeker":
            sk = dict(DEFAULTS["seeker"])
            sk.update(v or {})
            p["seeker"] = sk
        else:
            p[k] = v
    if not p["recipients"]:
        log("[error] profile has no recipients — nothing to email")
        sys.exit(2)
    if not p["queries"]:
        log("[error] profile has no queries — nothing to search")
        sys.exit(2)
    p["state_dir"] = Path(p["state_dir"]).expanduser()
    return p


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def find_himalaya() -> str | None:
    for cand in HIMALAYA_CANDIDATES:
        if "/" in cand:
            if Path(cand).exists():
                return cand
        else:
            try:
                r = subprocess.run(["which", cand], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get_json(url: str, timeout: int = 20) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"  [warn] {url.split('?')[0]} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Seen-jobs state (per profile, keyed by profile file name)
# ---------------------------------------------------------------------------

def seen_db_path(profile_path: str, cfg: dict) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(profile_path).stem.lower()).strip("-")
    return cfg["state_dir"] / f"seen-jobs-{slug}.json"


def load_seen(db: Path) -> set:
    if db.exists():
        try:
            return set(json.loads(db.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(db: Path, seen: set, cap: int = 5000) -> None:
    if len(seen) > cap:
        seen = set(list(seen)[-cap:])
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text(json.dumps(sorted(seen)))


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

def parse_salary(text: str) -> tuple[int | None, int | None, str | None]:
    if not text:
        return None, None, None
    t = text.replace("\n", " ")
    for pat, kind in SALARY_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        if kind == "range":
            lo, hi = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            return lo, hi, "listed range"
        if kind == "single":
            v = int(m.group(1).replace(",", ""))
            if 30000 <= v <= 400000:
                return v, v, "listed figure"
        if kind == "hourly-range":
            lo, hi = float(m.group(1)), float(m.group(2))
            return int(lo * 2080), int(hi * 2080), "hourly range annualized"
        if kind == "hourly":
            v = float(m.group(1))
            return int(v * 2080), int(v * 2080), "hourly annualized"
    return None, None, None


def normalize_annual(job: dict) -> tuple[int | None, int | None, str | None]:
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if lo or hi:
        return lo or None, hi or None, job.get("salary_note", "listed")
    smin, smax, note = parse_salary(job.get("description", ""))
    if smin or smax:
        return smin or None, smax or None, note or "parsed from description"
    return None, None, None


# ---------------------------------------------------------------------------
# Scam detection
# ---------------------------------------------------------------------------

def scam_flags(job: dict) -> list[str]:
    flags = []
    text = (job.get("description", "") + " " + job.get("title", "")).lower()
    if not job.get("company") or job.get("company", "").strip().lower() in ("confidential", "hiring", "n/a", "unknown", ""):
        flags.append("no identifiable employer")
    if not job.get("url"):
        flags.append("no application URL")
    lo, hi, _ = normalize_annual(job)
    if lo and lo >= 120000 and any(k in job.get("title", "").lower() for k in ("junior", "entry", "intern", "assistant", "specialist", "associate")):
        flags.append("compensation implausibly high for role level")
    if re.search(r"@(gmail|outlook|yahoo|hotmail|aol|mail)\.com", text):
        flags.append("application uses a personal email domain")
    for pat, label in SCAM_PATTERNS:
        if pat.search(text):
            flags.append(label)
    return flags


# ---------------------------------------------------------------------------
# Location gate
# ---------------------------------------------------------------------------

def make_location_gate(cfg: dict):
    allowed = [a.lower() for a in cfg["allowed_locations"]]
    remote_only = cfg["remote_only"]

    def location_allowed(job: dict) -> bool:
        loc = (job.get("location") or "").lower()
        if any(a in loc for a in allowed):
            return True
        if "remote" in loc:
            return True
        if job.get("remote_ok"):          # source is a remote-only job board
            return True
        desc = (job.get("description") or "").lower()
        if any(m in desc for m in HYBRID_MARKERS):
            return False                  # hybrid/onsite mention — not fully remote
        if any(p in desc for p in STRONG_REMOTE_PHRASES):
            return True
        return False

    def gate(job: dict) -> bool:
        if remote_only:
            return location_allowed(job)
        return True

    return gate


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------

def search_adzuna(query: str, location: str) -> list[dict]:
    env = load_env()
    aid, akey = env.get("ADZUNA_APP_ID", ""), env.get("ADZUNA_APP_KEY", "")
    if not aid or not akey:
        return []
    out = []
    for page in (1, 2):
        url = (f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
               f"?app_id={aid}&app_key={akey}&results_per_page=25"
               f"&what={urllib.parse.quote(query)}&where={urllib.parse.quote(location)}"
               "&content-type=application/json")
        data = http_get_json(url)
        if not data or not isinstance(data, dict):
            break
        for r in data.get("results", []):
            comp = r.get("company")
            company = comp.get("display_name", "Unknown") if isinstance(comp, dict) else (comp or "Unknown")
            loc = r.get("location")
            location_name = loc.get("display_name", "Remote") if isinstance(loc, dict) else "Remote"
            smin = r.get("salary_min") or None
            smax = r.get("salary_max") or None
            ad_id = str(r.get("id", ""))
            redirect = r.get("redirect_url", "")
            if "/land/ad/" in redirect:
                clean_url = f"https://www.adzuna.com/details/{ad_id}?utm_medium=api&utm_source={aid}"
            else:
                clean_url = redirect
            out.append({
                "title": r.get("title", ""),
                "company": company,
                "location": location_name,
                "url": clean_url,
                "salary_min": smin, "salary_max": smax,
                "salary_note": "listed" if (smin or smax) else "not listed",
                "source": "Adzuna",
                "description": (r.get("description") or "")[:3000],
                "remote_ok": "remote" in location_name.lower(),
            })
        time.sleep(1)
    return out


def search_serpapi(query: str, location: str) -> list[dict]:
    env = load_env()
    key = env.get("SERPAPI_KEY", "")
    if not key:
        return []
    out = []
    q = f"{query} in {location}" if location else query
    for start in (0, 10):
        url = (f"https://serpapi.com/search.json?engine=google_jobs"
               f"&q={urllib.parse.quote(q)}&hl=en&gl=us&api_key={key}&start={start}")
        data = http_get_json(url)
        if not data or not isinstance(data, dict):
            return out
        for r in data.get("jobs_results", []):
            det = r.get("detected_extensions") or {}
            smin, smax, note = parse_salary(det.get("salary") or "")
            out.append({
                "title": r.get("title", ""),
                "company": r.get("company_name", "Unknown"),
                "location": r.get("location", "Remote"),
                "url": r.get("share_link") or r.get("apply_link") or "",
                "salary_min": smin, "salary_max": smax, "salary_note": note or "not listed",
                "source": "Google Jobs (via SerpApi)",
                "description": (r.get("description") or "")[:3000],
            })
        time.sleep(0.5)
    return out


def _bulk_filter(rows: list[dict], relevant: list[str]) -> list[dict]:
    return [r for r in rows if any(k in r.get("title", "").lower() for k in relevant)]


def search_remotive(relevant: list[str]) -> list[dict]:
    out = []
    data = http_get_json("https://remotive.com/api/remote-jobs?limit=200")
    if not data or not isinstance(data, dict):
        return []
    for j in data.get("jobs", []):
        smin, smax, note = parse_salary(j.get("salary") or "")
        out.append({
            "title": j.get("title", ""),
            "company": j.get("company_name", "Unknown"),
            "location": j.get("candidate_required_location", "Remote"),
            "url": j.get("url", ""),
            "salary_min": smin, "salary_max": smax, "salary_note": note or "not listed",
            "source": "Remotive",
            "description": (j.get("description") or "")[:3000],
            "remote_ok": True,
        })
    return _bulk_filter(out, relevant)


def search_remoteok(relevant: list[str]) -> list[dict]:
    out = []
    data = http_get_json("https://remoteok.com/api")
    if not data or not isinstance(data, list):
        return []
    for j in data:
        if not isinstance(j, dict) or "slug" not in j:
            continue
        smin, smax = j.get("salary_min"), j.get("salary_max")
        if smin is None and j.get("salary"):
            smin, smax, _ = parse_salary(str(j.get("salary")))
        out.append({
            "title": j.get("position") or "",
            "company": j.get("company", "Unknown"),
            "location": "Remote",
            "url": f"https://remoteok.com/remote-jobs/{j.get('slug', '')}",
            "salary_min": smin, "salary_max": smax,
            "salary_note": "listed" if (smin or smax) else "not listed",
            "source": "RemoteOK",
            "description": str(j.get("description") or "")[:3000],
            "remote_ok": True,
        })
    return _bulk_filter(out, relevant)


def search_jobicy(relevant: list[str]) -> list[dict]:
    out = []
    data = http_get_json("https://jobicy.com/api/v2/remote-jobs?count=50")
    if not data or not isinstance(data, dict):
        return []
    for j in data.get("jobs", []):
        smin, smax = j.get("jobSalaryMin"), j.get("jobSalaryMax")
        if smin is None and j.get("jobSalary"):
            smin, smax, _ = parse_salary(str(j.get("jobSalary")))
        smin = int(smin) if smin else None
        smax = int(smax) if smax else None
        out.append({
            "title": j.get("jobTitle") or "",
            "company": j.get("companyName", "Unknown"),
            "location": j.get("jobGeo", "Remote"),
            "url": j.get("url", ""),
            "salary_min": smin, "salary_max": smax,
            "salary_note": "listed" if (smin or smax) else "not listed",
            "source": "Jobicy",
            "description": str(j.get("jobDescription") or "")[:3000],
            "remote_ok": True,
        })
    return _bulk_filter(out, relevant)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def make_scorer(cfg: dict):
    strong = cfg["strong_title_keywords"]
    tools = cfg["tool_keywords"]
    allowed = [a.lower() for a in cfg["allowed_locations"]]

    def score_job(job: dict) -> int:
        score = 0
        title = job.get("title", "").lower()
        desc = (job.get("description") or "").lower()
        loc = job.get("location", "").lower()
        if any(k in title for k in strong):
            score += 40
        if any(k in (title + " " + desc) for k in tools):
            score += 15
        if "remote" in loc:
            score += 10
        if any(a in loc for a in allowed):
            score += 8
        if job.get("salary_min") or job.get("salary_max"):
            score += 5
        flags = scam_flags(job)
        if flags:
            score -= 60 * len(flags)
        return score

    return score_job


# ---------------------------------------------------------------------------
# Brief rendering
# ---------------------------------------------------------------------------

def fmt_salary(job: dict) -> str:
    lo, hi, note = normalize_annual(job)
    if lo is None and hi is None:
        return "salary not listed"
    if lo and hi and lo != hi:
        return f"${lo:,} – ${hi:,}/yr ({note})"
    if lo:
        return f"${lo:,}+/yr ({note})"
    if hi:
        return f"up to ${hi:,}/yr ({note})"
    return "salary not listed"


def render_brief(jobs: list[dict], flagged: list[tuple[dict, list[str]]], cfg: dict) -> str:
    today = date.today().strftime("%A, %B %-d, %Y")
    name = cfg["seeker"].get("name", "")
    header = f"Daily job brief — {today}"
    if name:
        header += f" — {name}"
    lines = [header]
    if cfg["min_salary_usd"]:
        lines.append(f"Minimum salary filter: ${cfg['min_salary_usd']:,}/yr")
    notes = cfg["seeker"].get("notes", "")
    if notes:
        lines.append(f"Profile notes: {notes}")
    lines.append("")
    if not jobs:
        lines.append("No new qualifying jobs found today.")
    else:
        for i, j in enumerate(jobs, 1):
            lines.append(f"{i}. {j['title']}")
            lines.append(f"   {j['company']}  |  {j['location']}")
            lines.append(f"   {fmt_salary(j)}")
            lines.append(f"   Source: {j['source']}")
            lines.append(f"   {j['url']}")
            lines.append("")
    if flagged:
        lines.append("--- Jobs to avoid (scam/red-flag warning) ---")
        for j, flags in flagged:
            lines.append(f"! {j['title']}  —  {j['company']}")
            for f in flags:
                lines.append(f"    ⚠ {f}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, cfg: dict) -> bool:
    himalaya = find_himalaya()
    if not himalaya:
        log("[error] himalaya not found — install it and configure an account (see himalaya skill)")
        return False
    account = cfg["email_from"]
    env = load_env()
    from_addr = env.get("JOB_BRIEF_FROM", "")
    for rcpt in cfg["recipients"]:
        from_header = f"From: {from_addr}\r\n" if from_addr else ""
        msg = f"{from_header}To: {rcpt}\r\nSubject: {subject}\r\n\r\n{body}\r\n"
        cmd = [himalaya, "message", "send"]
        if account:
            cmd += ["-a", account]
        p = subprocess.run(cmd, input=msg, text=True, capture_output=True, timeout=90)
        if p.returncode != 0:
            log(f"[error] email to {rcpt} failed: {p.stderr.strip()}")
            return False
        log(f"  email sent to {rcpt}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print(__doc__)
        sys.exit(2)
    profile_path = args[0]
    flags = set(args[1:])
    dry_run = "--dry-run" in flags
    test_email = "--test" in flags

    cfg = load_profile(profile_path)
    relevant = [k.lower() for k in cfg["relevant_title_keywords"]]
    exclude_titles = [k.lower() for k in cfg["exclude_title_keywords"]]
    exclude_locs = [k.lower() for k in cfg["exclude_location_keywords"]]
    location_ok = make_location_gate(cfg)
    score_job = make_scorer(cfg)
    seen_db = seen_db_path(profile_path, cfg)

    if test_email:
        ok = send_email(f"{cfg['subject_prefix']} test email",
                        "If you see this, himalaya SMTP is working.", cfg)
        print("test email:", "OK" if ok else "FAILED", file=sys.stderr)
        sys.exit(0 if ok else 1)

    env = load_env()
    src = cfg["sources"]
    if src.get("adzuna") and (not env.get("ADZUNA_APP_ID") or not env.get("ADZUNA_APP_KEY")):
        log("  [warn] Adzuna enabled but keys missing (ADZUNA_APP_ID/ADZUNA_APP_KEY in ~/.hermes/.env)")
    if src.get("serpapi") and not env.get("SERPAPI_KEY"):
        log("  [info] SerpApi enabled but SERPAPI_KEY not set — skipped")

    seen = load_seen(seen_db)
    candidates: dict[str, dict] = {}
    seen_dups: set[tuple[str, str]] = set()

    def consider(j: dict, use_relevance_gate: bool) -> None:
        key = (j.get("url") or j.get("title", "") + "|" + j.get("company", "")).strip()
        if not key or key in seen:
            return
        title_lower = j.get("title", "").lower()
        desc_lower = title_lower + " " + (j.get("description") or "").lower()
        loc_lower = (j.get("location") or "").lower()
        haystack = title_lower + " " + loc_lower + " " + desc_lower
        if any(k in title_lower for k in exclude_titles):
            return
        if any(k in haystack for k in exclude_locs):
            return
        if not location_ok(j):
            return
        if use_relevance_gate:
            title_hit = any(k in title_lower for k in relevant)
            tool_hit = any(k in desc_lower for k in cfg["tool_keywords"])
            if not (title_hit or tool_hit):
                return
        dup_key = (j["title"].strip().lower(), j.get("company", "").strip().lower())
        if dup_key in seen_dups:
            return
        seen_dups.add(dup_key)
        candidates[key] = j

    # --- per-query sources ---
    per_query = []
    if src.get("adzuna"):
        per_query.append(("Adzuna", search_adzuna))
    if src.get("serpapi"):
        per_query.append(("Google Jobs (SerpApi)", search_serpapi))
    for name, fn in per_query:
        for q in cfg["queries"]:
            query, location = q[0], q[1] if len(q) > 1 else ""
            try:
                jobs = fn(query, location)
            except Exception as e:
                log(f"  [warn] {name}/{query} errored: {e}")
                continue
            log(f"  {name}/{query}: {len(jobs)}")
            for j in jobs:
                consider(j, use_relevance_gate=True)

    # --- bulk sources (fetch once, pre-filtered client-side by relevant titles) ---
    bulk = []
    if src.get("remotive"):
        bulk.append(("Remotive", search_remotive))
    if src.get("remoteok"):
        bulk.append(("RemoteOK", search_remoteok))
    if src.get("jobicy"):
        bulk.append(("Jobicy", search_jobicy))
    for name, fn in bulk:
        try:
            jobs = fn(relevant)
        except Exception as e:
            log(f"  [warn] {name} errored: {e}")
            continue
        log(f"  {name}: {len(jobs)} relevant")
        for j in jobs:
            consider(j, use_relevance_gate=False)

    log(f"found {len(candidates)} new candidate postings")

    flagged: list[tuple[dict, list[str]]] = []
    qualified: list[tuple[int, dict]] = []
    min_salary = cfg["min_salary_usd"]
    dropped_low_salary = 0
    for key, j in candidates.items():
        flags = scam_flags(j)
        if flags:
            flagged.append((j, flags))
            continue
        lo, hi, _ = normalize_annual(j)
        if lo is not None and lo < min_salary:
            dropped_low_salary += 1
            continue
        if lo is None and hi is not None and hi < min_salary:
            dropped_low_salary += 1
            continue
        qualified.append((score_job(j), j))
    if dry_run:
        log(f"  [dry] {len(qualified)} qualified, {len(flagged)} flagged, {dropped_low_salary} below salary floor")

    qualified.sort(key=lambda x: x[0], reverse=True)
    top = [j for _, j in qualified[:cfg["max_jobs"]]]

    brief = render_brief(top, flagged[:5], cfg)
    subject = f"{cfg['subject_prefix']} {date.today().strftime('%b %-d')} — {len(top)} new matches"

    if dry_run:
        print(brief)
        print(f"\n--- {len(flagged)} flagged postings excluded ---")
        for j, flags in flagged:
            print(f"  ! {j['title']} @ {j['company']}: {', '.join(flags)}")
        save_seen(seen_db, seen | set(candidates.keys()))
        sys.exit(0)

    # Only mark jobs as seen AFTER a successful send — if the email fails,
    # the postings stay unseen and the next run retries them.
    ok = send_email(subject, brief, cfg)
    if not ok:
        log("[error] email delivery failed — jobs NOT marked seen; will retry next run")
        sys.exit(1)
    save_seen(seen_db, seen | set(candidates.keys()))
    print(f"brief sent: {len(top)} jobs, {len(flagged)} flagged excluded", file=sys.stderr)


if __name__ == "__main__":
    main()
