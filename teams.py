#!/usr/bin/env python3
"""Hiring Team Radar.

Finds new engineering job posts at your target companies, groups them into teams
(AI/ML, Data, Security, Infrastructure, ...), adds each company's headcount trend,
and flags the teams that grew fast and are still hiring, with a one-line summary
per team for Slack.

Data layer: Crustdata POST /job/search and POST /company/enrich, plus an optional
POST /watch/job/search discovery watch (https://docs.crustdata.com).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

DEFAULT_COMPANIES = "datadog.com,cloudflare.com,snowflake.com"
DEFAULT_KEYWORDS = "engineer,developer"
DEFAULT_EXCLUDE_TEAMS = "Sales & Solutions Engineering"
GROWTH_WINDOWS = ("mom", "qoq", "six_months", "yoy")

# First match wins, so sales-side titles are caught before "engineer" topics.
TEAM_RULES = [
    ("Sales & Solutions Engineering", re.compile(r"\b(solutions?|sales|pre-?sales|customer|support|field|technical account|partner|implementation|forward deployed)\b", re.I)),
    ("AI & ML", re.compile(r"\b(machine learning|ml|ai|deep learning|data scientist|applied scientist|research)\b", re.I)),
    ("Data", re.compile(r"\b(data|analytics|etl|database)\b", re.I)),
    ("Security", re.compile(r"\b(security|cyber|appsec|detection|threat)\b", re.I)),
    ("Infrastructure & Platform", re.compile(r"\b(infrastructure|platform|sre|site reliability|devops|cloud|systems|network|kubernetes|reliability)\b", re.I)),
    ("Frontend & Mobile", re.compile(r"\b(front-?end|ui|ux|mobile|ios|android|web)\b", re.I)),
]
DEFAULT_TEAM = "Software Engineering"

JOB_FIELDS = [
    "crustdata_job_id",
    "job_details.title",
    "job_details.url",
    "job_details.reposted_job",
    "company.basic_info.crustdata_company_id",
    "company.basic_info.name",
    "company.basic_info.primary_domain",
    "location.country",
    "metadata.date_added",
]
COMPANY_FIELDS = ["basic_info", "headcount"]


def job_filters(domains: List[str], keywords: List[str], since: str) -> Dict[str, Any]:
    return {"op": "and", "conditions": [
        {"field": "company.basic_info.primary_domain", "type": "in", "value": domains},
        {"op": "or", "conditions": [
            {"field": "job_details.title", "type": "[.]", "value": k} for k in keywords]},
        {"field": "metadata.date_added", "type": "=>", "value": since},
    ]}


def classify_team(title: str) -> str:
    for team, pattern in TEAM_RULES:
        if pattern.search(title or ""):
            return team
    return DEFAULT_TEAM


def group_teams(jobs: List[Dict[str, Any]], exclude_teams: List[str]) -> Dict[str, Any]:
    """Group postings into (company, team). Drops reposts, duplicates and excluded teams."""
    teams: Dict[Any, Dict[str, Any]] = {}
    seen_ids = set()
    counts = Counter()
    for job in jobs:
        details = job.get("job_details") or {}
        info = (job.get("company") or {}).get("basic_info") or {}
        job_id = job.get("crustdata_job_id")
        company_id = info.get("crustdata_company_id")
        if not company_id:
            counts["no_company"] += 1
            continue
        if details.get("reposted_job"):
            counts["reposts"] += 1
            continue
        if job_id in seen_ids:
            counts["duplicates"] += 1
            continue
        seen_ids.add(job_id)
        team = classify_team(details.get("title", ""))
        if team in exclude_teams:
            counts["excluded_team"] += 1
            continue
        t = teams.setdefault((company_id, team), {
            "company_id": company_id,
            "company": info.get("name", ""),
            "domain": normalize_domain(info.get("primary_domain")),
            "team": team,
            "posts": 0, "titles": [], "countries": set(), "latest_post": "", "job_urls": [], "job_ids": [],
        })
        t["posts"] += 1
        t["titles"].append(details.get("title", ""))
        t["job_ids"].append(job_id)
        t["countries"].add((job.get("location") or {}).get("country") or "")
        if details.get("url"):
            t["job_urls"].append(details["url"])
        t["latest_post"] = max(t["latest_post"], day((job.get("metadata") or {}).get("date_added")))
    out = sorted(teams.values(), key=lambda t: (t["posts"], t["latest_post"]), reverse=True)
    for t in out:
        t["countries"] = sorted(c for c in t["countries"] if c)
    return {"teams": out, "dropped": dict(counts)}


def add_headcount(teams: List[Dict[str, Any]], companies: List[Dict[str, Any]], window: str) -> None:
    """Attach company headcount and growth for `window` (mom, qoq, six_months, yoy)."""
    by_id = {c.get("crustdata_company_id"): c for c in companies}
    for t in teams:
        hc = (by_id.get(t["company_id"]) or {}).get("headcount") or {}
        growth = (hc.get("growth_percent") or {}).get(window)
        t["headcount"] = hc.get("total")
        t["growth_pct"] = round(growth, 1) if isinstance(growth, (int, float)) else None


def flag_teams(teams: List[Dict[str, Any]], min_growth: float, min_posts: int) -> None:
    """Flag = company grew at least `min_growth` percent AND the team has `min_posts` new posts."""
    for t in teams:
        grew = t.get("growth_pct") is not None and t["growth_pct"] >= min_growth
        t["flagged"] = bool(grew and t["posts"] >= min_posts)
    teams.sort(key=lambda t: (t["flagged"], t["posts"], t.get("growth_pct") or -999), reverse=True)


def one_liner(t: Dict[str, Any], window: str) -> str:
    growth = "headcount trend unknown" if t.get("growth_pct") is None else \
        f"company headcount {t['growth_pct']:+.1f}% ({window})"
    size = f", {t['headcount']:,} people" if t.get("headcount") else ""
    plural = "post" if t["posts"] == 1 else "posts"
    return f"{t['company']} {t['team']}: {t['posts']} new {plural}, {growth}{size}."


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    teams = result["teams"]
    flagged = [t for t in teams if t.get("flagged")]
    return {
        "job_posts": result["posts"],
        "companies": len({t["company_id"] for t in teams}),
        "teams": len(teams),
        "flagged_teams": len(flagged),
        "flagged_by_team": dict(Counter(t["team"] for t in flagged).most_common()),
        "dropped": result["dropped"],
    }


def slack_text(result: Dict[str, Any], since: str, window: str, limit: int = 12) -> str:
    flagged = [t for t in result["teams"] if t.get("flagged")]
    lines = [f":busts_in_silhouette: {len(flagged)} teams to recruit from first (new engineering posts since {since})"]
    for t in flagged[:limit]:
        lines.append(f"• {t.get('summary') or one_liner(t, window)}")
    if len(flagged) > limit:
        lines.append(f"…and {len(flagged) - limit} more in out/teams.csv")
    if not flagged:
        lines.append("No team met the growth and hiring thresholds this week.")
    return "\n".join(lines)


def write_outputs(result: Dict[str, Any], out_dir: str, window: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "teams.json"), "w") as fh:
        json.dump({"summary": summary(result), **result}, fh, indent=1)
    with open(os.path.join(out_dir, "teams.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["flagged", "company", "domain", "team", "posts", "latest_post", "headcount",
                    f"growth_pct_{window}", "titles", "job_urls", "summary"])
        for t in result["teams"]:
            w.writerow([t["flagged"], t["company"], t["domain"], t["team"], t["posts"], t["latest_post"],
                        t.get("headcount"), t.get("growth_pct"), "; ".join(t["titles"]), "; ".join(t["job_urls"]),
                        t.get("summary") or one_liner(t, window)])


def only_new_jobs(jobs: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = _load_state(state_path)
    return [j for j in jobs if str(j.get("crustdata_job_id")) not in seen]


def save_state(teams: List[Dict[str, Any]], state_path: str) -> None:
    seen = _load_state(state_path)
    seen.update(str(i) for t in teams for i in t["job_ids"])
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def _load_state(path: str) -> set:
    if os.path.exists(path):
        with open(path) as fh:
            return set(json.load(fh))
    return set()


def split(value: str) -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Hiring Team Radar (Crustdata data layer)")
    p.add_argument("--companies", default=os.environ.get("COMPANIES", DEFAULT_COMPANIES), help="target company domains, comma list")
    p.add_argument("--keywords", default=os.environ.get("KEYWORDS", DEFAULT_KEYWORDS), help="words a job title must contain, comma list")
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "7")))
    p.add_argument("--window", default=os.environ.get("GROWTH_WINDOW", "qoq"), choices=GROWTH_WINDOWS, help="headcount growth window")
    p.add_argument("--min-growth", type=float, default=float(os.environ.get("MIN_GROWTH", "5")), help="percent over the window")
    p.add_argument("--min-posts", type=int, default=int(os.environ.get("MIN_POSTS", "2")), help="new posts per team")
    p.add_argument("--exclude-teams", default=os.environ.get("EXCLUDE_TEAMS", DEFAULT_EXCLUDE_TEAMS), help='team names to drop; "" keeps all')
    p.add_argument("--max-jobs", type=int, default=1000)
    p.add_argument("--summaries", action="store_true", help="rewrite each flagged line with Claude")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack")
    p.add_argument("--all", action="store_true", help="include posts already alerted on")
    p.add_argument("--create-watch", action="store_true", help="create a weekly Crustdata job watch for these filters, then exit")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    domains = [normalize_domain(d) for d in split(args.companies)]
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    filters = job_filters(domains, split(args.keywords), since)
    client = Crustdata()
    if args.create_watch:
        print(json.dumps(client.create_job_watch(filters), indent=1))
        return 0

    jobs = list(client.job_search(filters, JOB_FIELDS, max_results=args.max_jobs,
                                  sorts=[{"field": "metadata.date_added", "order": "desc"}]))
    posts = len(jobs)
    if not args.all:
        jobs = only_new_jobs(jobs, args.state)
    result = {"posts": posts, **group_teams(jobs, split(args.exclude_teams))}
    if result["teams"]:
        ids = sorted({t["company_id"] for t in result["teams"]})
        add_headcount(result["teams"], client.company_enrich(ids, COMPANY_FIELDS), args.window)
    flag_teams(result["teams"], args.min_growth, args.min_posts)
    if args.summaries:
        if not claude_available():
            print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): using plain summaries", file=sys.stderr)
        else:
            for t in result["teams"]:
                if t["flagged"]:
                    t["summary"] = ask_claude(
                        f"Facts: {one_liner(t, args.window)} Sample titles: {'; '.join(t['titles'][:4])}. "
                        "Write one plain sentence (max 30 words) telling a recruiter why to reach out to "
                        "this team first. Use only these facts. No hype, no emojis. Return only the sentence.") or None
    write_outputs(result, args.out, args.window)
    print(json.dumps(summary(result), indent=1))
    if any(t["flagged"] for t in result["teams"]):
        print(send_slack(slack_text(result, since, args.window), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(result["teams"], args.state)
    else:
        print("No flagged teams.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
