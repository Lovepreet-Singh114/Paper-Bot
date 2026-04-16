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
JOURNALS = CFG["journals_crossref"]
TOP_N = CFG.get("top_n", 2)
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


# -------- Matching logic --------
def match_keywords(text, kw_list):
    text = (text or "").lower()
    return [kw for kw in kw_list if kw in text]


def evaluate(title, abstract):
    """
    Rules:
      - Passes if matches any genomic keyword (any crop OK), OR
      - Passes if matches bean keyword AND at least one other keyword
    """
    blob = f"{title} {abstract}".lower()
    genomic_hits = match_keywords(blob, GENOMIC_KW)
    bean_hits = match_keywords(blob, BEAN_KW)
    other_hits = match_keywords(blob, OTHER_KW)

    if genomic_hits:
        score = len(genomic_hits) * 2 + len(bean_hits) * 2 + len(other_hits)
        return True, score
    if bean_hits and other_hits:
        score = len(bean_hits) * 2 + len(other_hits)
        return True, score
    return False, 0


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
                passes, score = evaluate(title, abstract)
                if passes:
                    results.append({
                        "title": title,
                        "abstract": abstract,
                        "link": link,
                        "source": journal,
                        "score": score,
                    })
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
            passes, score = evaluate(e.title, e.summary)
            if passes:
                results.append({
                    "title": e.title,
                    "abstract": e.summary,
                    "link": e.link,
                    "source": "arXiv",
                    "score": score,
                })
    except Exception as ex:
        print(f"arXiv error: {ex}", file=sys.stderr)
    return results


# -------- Slack --------
def post_slack(papers):
    if not papers:
        requests.post(SLACK_WEBHOOK, json={"text": "📭 No new papers matching your keywords today."})
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
    all_papers = fetch_crossref() + fetch_arxiv()

    run_seen, unique = set(), []
    for p in all_papers:
        pid = paper_id(p)
        if pid in run_seen or pid in seen:
            continue
        run_seen.add(pid)
        unique.append(p)

    top = sorted(unique, key=lambda x: x["score"], reverse=True)[:TOP_N]
    post_slack(top)

    for p in top:
        seen.add(paper_id(p))
    save_seen(seen)


if __name__ == "__main__":
    main()
