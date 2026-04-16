import os
import re
import sys
import yaml
import time
import json
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

# -------- Load config --------
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

GENOMIC_KW = [k.lower() for k in CFG["genomic_keywords"]]
BEAN_KW = [k.lower() for k in CFG["bean_keywords"]]
OTHER_KW = [k.lower() for k in CFG["other_keywords"]]
PLANT_KW = [k.lower() for k in CFG["plant_keywords"]]
PLANT_JOURNALS = {j.lower() for j in CFG.get("plant_journals", [])}
JOURNALS = CFG["journals_crossref"]
LOOKBACK = CFG.get("lookback_hours", 72)

SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]

CUTOFF = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK)
SEEN_FILE = "seen.json"

ALL_KW = GENOMIC_KW + BEAN_KW + OTHER_KW


# -------- Seen tracking --------
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    trimmed = list(seen)[-2000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


def paper_id(p):
    return (p.get("link") or p.get("title", "")).strip().lower()


# -------- Matching --------
def match_keywords(text, kw_list):
    text = (text or "").lower()
    return [kw for kw in kw_list if kw in text]


def has_plant_context(p):
    """Return True if paper is plant-related."""
    if p.get("source", "").lower() in PLANT_JOURNALS:
        return True
    blob = f"{p.get('title','')} {p.get('abstract','')}".lower()
    return bool(match_keywords(blob, PLANT_KW))


def tag_paper(p):
    blob = f"{p.get('title','')} {p.get('abstract','')}".lower()
    p["_genomic_hits"] = match_keywords(blob, GENOMIC_KW)
    p["_bean_hits"] = match_keywords(blob, BEAN_KW)
    p["_other_hits"] = match_keywords(blob, OTHER_KW)
    p["score"] = (
        len(p["_genomic_hits"]) * 2
        + len(p["_bean_hits"]) * 2
        + len(p["_other_hits"])
    )
    return p


def has_any_keyword(p):
    return bool(p["_genomic_hits"] or p["_bean_hits"] or p["_other_hits"])


# -------- Crossref --------
def fetch_crossref():
    results = []
    from_date = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK)).strftime("%Y-%m-%d")
    for journal in JOURNALS:
        url = (
            f"https://api.crossref.org/works?query.container-title={quote_plus(journal)}"
            f"&filter=from-pub-date:{from_date}&rows=20&sort=published&order=desc"
        )
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "paper-bot/1.0"})
            items = r.json().get("message", {}).get("items", [])
            for it in items:
                title = " ".join(it.get("title", [""]))
                abstract = re.sub(r"<[^>]+>", "", it.get("abstract", "") or "")
                doi = it.get("DOI", "")
                link = f"https://doi.org/{doi}" if doi else it.get("URL", "")
                p = tag_paper({
                    "title": title,
                    "abstract": abstract,
                    "link": link,
                    "source": journal,
                })
                if has_any_keyword(p) and has_plant_context(p):
                    results.append(p)
        except Exception as e:
            print(f"Crossref error for {journal}: {e}", file=sys.stderr)
        time.sleep(0.5)
    return results


# -------- arXiv --------
def fetch_arxiv():
    results = []
    query = " OR ".join([f'all:"{kw}"' for kw in ALL_KW])
    url = f"http://export.arxiv.org/api/query?search_query={quote_plus(query)}&sortBy=submittedDate&sortOrder=descending&max_results=50"
    try:
        feed = feedparser.parse(url)
        for e in feed.entries:
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            if pub < CUTOFF:
                continue
            p = tag_paper({
                "title": e.title,
                "abstract": e.summary,
                "link": e.link,
                "source": "arXiv",
            })
            if has_any_keyword(p) and has_plant_context(p):
                results.append(p)
    except Exception as ex:
        print(f"arXiv error: {ex}", file=sys.stderr)
    return results


# -------- bioRxiv --------
def fetch_biorxiv():
    results = []
    end = datetime.now(timezone.utc).date()
    start = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK)).date()
    try:
        cursor = 0
        for _ in range(5):
            page_url = f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}"
            r = requests.get(page_url, timeout=30)
            data = r.json()
            papers = data.get("collection", [])
            if not papers:
                break
            for it in papers:
                title = it.get("title", "")
                abstract = it.get("abstract", "")
                doi = it.get("doi", "")
                link = f"https://doi.org/{doi}" if doi else ""
                p = tag_paper({
                    "title": title,
                    "abstract": abstract,
                    "link": link,
                    "source": "bioRxiv",
                })
                if has_any_keyword(p) and has_plant_context(p):
                    results.append(p)
            cursor += len(papers)
            if len(papers) < 100:
                break
    except Exception as ex:
        print(f"bioRxiv error: {ex}", file=sys.stderr)
    return results


# -------- Slot-based selection --------
def pick_by_slots(papers):
    """
    Strict slot rule:
      - Slot 1: genomic keyword, non-bioRxiv preferred (fall back to any)
      - Slot 2: bean keyword, non-bioRxiv preferred (fall back to any)
      - Slot 3: bioRxiv (any keyword)
    Each slot can be empty if no match exists.
    """
    picked = []
    picked_ids = set()

    def take(candidates):
        for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
            if paper_id(c) not in picked_ids:
                picked.append(c)
                picked_ids.add(paper_id(c))
                return True
        return False

    # Slot 1: genomic
    non_biorxiv_genomic = [p for p in papers if p["_genomic_hits"] and p["source"] != "bioRxiv"]
    if not take(non_biorxiv_genomic):
        take([p for p in papers if p["_genomic_hits"]])

    # Slot 2: bean
    non_biorxiv_bean = [p for p in papers if p["_bean_hits"] and p["source"] != "bioRxiv"]
    if not take(non_biorxiv_bean):
        take([p for p in papers if p["_bean_hits"]])

    # Slot 3: bioRxiv
    take([p for p in papers if p["source"] == "bioRxiv"])

    return picked


# -------- Slack --------
def post_slack(papers):
    if not papers:
        requests.post(SLACK_WEBHOOK, json={"text": "📭 No new plant papers matching your keywords today."})
        return

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "📚 Daily Research Digest"}}]
    for p in papers:
        abstract = p.get("abstract") or "_No abstract available._"
        if len(abstract) > 600:
            abstract = abstract[:600].rsplit(" ", 1)[0] + "…"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{p['link']}|{p['title']}>*\n_Source: {p['source']}_\n\n{abstract}",
            },
        })
        blocks.append({"type": "divider"})
    requests.post(SLACK_WEBHOOK, json={"blocks": blocks})


# -------- Main --------
def main():
    seen = load_seen()
    all_papers = fetch_crossref() + fetch_arxiv() + fetch_biorxiv()

    run_seen, unique = set(), []
    for p in all_papers:
        pid = paper_id(p)
        if pid in run_seen or pid in seen:
            continue
        run_seen.add(pid)
        unique.append(p)

    top = pick_by_slots(unique)
    post_slack(top)

    for p in top:
        seen.add(paper_id(p))
    save_seen(seen)


if __name__ == "__main__":
    main()
