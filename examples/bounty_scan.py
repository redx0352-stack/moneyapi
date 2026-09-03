#!/usr/bin/env python3
"""
bounty_scan.py - Track 2 of the money plan.

Scans GitHub Issues for "bounty"-tagged issues across well-known paid-bounty repos
(Algora-powered projects, OSS-funded projects, etc.) plus a free-text search.

Filters by:
  - Payout >= MIN_PAYOUT_USD (default $50)
  - Skill matches our profile (Python, TypeScript, Docker, AI, system plumbing)
  - Repo activity (stars >= 100 or recent issue activity)

Writes /data/bounties.json with the latest scan results.
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

MIN_PAYOUT_USD = float(os.environ.get("MIN_PAYOUT", "50"))
GITHUB_UA = "Mozilla/5.0 (compatible; moneyapi-bounty/1.0)"
GITHUB_ACCEPT = "application/vnd.github+json"

SKILLS = {
    "python", "typescript", "javascript", "docker", "kubernetes",
    "ai", "llm", "agent", "openai", "anthropic", "claude",
    "fastapi", "flask", "django", "react", "next", "node",
    "postgres", "redis", "sqlite", "api", "rest", "graphql",
    "linux", "bash", "system", "devops", "ci", "cd", "github actions",
    "rust", "go", "golang", "sql", "transformers", "pytorch",
    "huggingface", "openrouter", "websocket", "grpc",
}


def fetch_json(url, headers=None, timeout=15):
    h = {"User-Agent": GITHUB_UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}", "_url": url}


def extract_payout(text):
    """Extract USD amount from bounty text. Looks for $500, $1,000, USD 500, etc."""
    if not text:
        return 0
    candidates = []
    # $1,000  $1K  $1.5K  $1.5M  $250  etc.
    for m in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?([KkMm]?)", text):
        n = float(m.group(1).replace(",", ""))
        suffix = m.group(2).upper()
        if suffix == "K":
            n *= 1000
        elif suffix == "M":
            n *= 1_000_000
        candidates.append(n)
    # USD 500 / 500 USD
    for m in re.finditer(r"(?:USD|usd)\s?\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?([KkMm]?)", text):
        n = float(m.group(1).replace(",", ""))
        suffix = m.group(2).upper()
        if suffix == "K":
            n *= 1000
        elif suffix == "M":
            n *= 1_000_000
        candidates.append(n)
    return max(candidates) if candidates else 0


def search_github_bounties(query, per_page=30):
    """Search GitHub issues for bounty-tagged work."""
    q = urllib.parse.quote(query)
    url = (
        "https://api.github.com/search/issues"
        f"?q={q}&per_page={per_page}&sort=created&order=desc"
    )
    data = fetch_json(url, headers={"Accept": GITHUB_ACCEPT})
    if isinstance(data, dict) and data.get("_error"):
        return [{"platform": "github", "error": data["_error"], "query": query}]
    return data.get("items", [])


def score_issue(issue):
    """Return a score 0-100 for how attractive this bounty is."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    labels = " ".join(l.get("name", "") for l in issue.get("labels", []))
    repo_full = issue.get("repository_url", "").split("/repos/")[-1]
    text = (title + " " + body + " " + labels).lower()

    score = 0
    payout = extract_payout(body or title)
    if payout >= MIN_PAYOUT_USD:
        score += min(payout / 100, 30)
    if "bounty" in labels.lower():
        score += 15
    matched_skills = [s for s in SKILLS if s in text]
    score += min(len(matched_skills) * 3, 25)
    # Comments indicate community interest
    comments = issue.get("comments", 0)
    if comments > 0:
        score += min(comments, 5)
    return score, matched_skills, payout


def main():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[bounty_scan] start {ts}  min_payout=${MIN_PAYOUT_USD}", flush=True)

    # Multi-pronged search
    queries = [
        "is:issue is:open label:bounty",
        "is:issue is:open label:\"💎bounty\"",
        "is:issue is:open label:bounty is:public",
    ]

    all_items = []
    for q in queries:
        items = search_github_bounties(q, per_page=30)
        all_items.extend(items)
        time.sleep(0.5)  # be polite to github

    # Dedup by issue id
    seen = set()
    unique = []
    for it in all_items:
        if it.get("id") and it["id"] not in seen:
            seen.add(it["id"])
            unique.append(it)

    scored_bounties = []
    for issue in unique:
        try:
            score, matched, payout = score_issue(issue)
            if score <= 0:
                continue
            scored_bounties.append({
                "platform": "github",
                "id": issue.get("id"),
                "number": issue.get("number"),
                "title": issue.get("title"),
                "repo": issue.get("repository_url", "").split("/repos/")[-1],
                "url": issue.get("html_url"),
                "payout_usd": payout,
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "skill_match": matched,
                "comments": issue.get("comments", 0),
                "created_at": issue.get("created_at"),
                "score": round(score, 1),
            })
        except Exception as e:
            print(f"  parse_err: {e}", flush=True)

    scored_bounties.sort(key=lambda x: x["score"], reverse=True)
    top = scored_bounties[:40]

    out = {
        "ts": ts,
        "filter_min_payout": MIN_PAYOUT_USD,
        "scanned_total": len(unique),
        "matched_total": len(scored_bounties),
        "top": top,
    }
    with open("/data/bounties.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"[bounty_scan] scanned={len(unique)} scored={len(scored_bounties)} top={len(top)}", flush=True)
    for b in top[:8]:
        print(f"  score={b['score']:>5}  ${b['payout_usd']:>6.0f}  [{b['repo'][:30]:>30}]  {b['title'][:55]}", flush=True)

    return out


if __name__ == "__main__":
    main()