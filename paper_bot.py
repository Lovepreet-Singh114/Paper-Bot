import os
import re
import sys
import yaml
import json
import time
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from anthropic import Anthropic

# -------- Load config --------
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

KEYWORDS = [k.lower() for k in CFG["keywords"]]
JOURNALS = CFG["journals_crossref"]
TOP_N = CFG.get("top_n", 2)
LOOKBACK = CFG.get("lookback_hours", 24)

SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

client = Anthropic(api_key=ANTHROPIC_KEY)
CUTOFF = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK)


# -------- Scoring --------
def score(text):
    text = (text or "").lower()
    return sum(1 for kw in KEYWORDS if kw in text)


# -------- Crossref (journals) --------
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
                s = score(title + " " + abstract)
                if s > 0:
                    results.append({
                        "title": title,
                        "abstract": abstract,
                        "link": link,
                        "source": journal,
                        "score": s,
                    })
        except Exception as e:
            print(f"Crossref error for {journal}: {e}", file=sys.stderr)
        time.sleep(0.5)
    return results


# -------- arXiv --------
def fetch_arxiv():
    results = []
    query = " OR ".join([f'all:"{kw}"' for kw in KEYWORDS])
    url = f"http://export.arxiv.org/api/query?search_query={quote_plus(query)}&sortBy=submittedDate&sortOrder=descending&max_results=30"
    try:
        feed = feedparser.parse(url)
        for e in feed.entries:
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            if pub < CUTOFF:
                continue
            title = e.title
            abstract = e.summary
            s = score(title + " " + abstract)
            if s > 0:
                results.append({
                    "title": title,
                    "abstract": abstract,
                    "link": e.link,
                    "source": "arXiv",
                    "score": s,
                })
    except Exception as ex:
        print(f"arXiv error: {ex}", file=sys.stderr)
    return results


# -------- Google Scholar --------
def fetch_scholar():
    results = []
    try:
        from scholarly import scholarly
        for kw in KEYWORDS[:4]:  # limit queries to avoid blocking
            try:
                search = scholarly.search_pubs(kw, year_low=datetime.now().year)
                for _ in range(3):
                    pub = next(search, None)
                    if not pub:
                        break
                    bib = pub.get("bib", {})
                    title = bib.get("title", "")
                    abstract = bib.get("abstract", "")
                    link = pub.get("pub_url", "")
                    s = score(title + " " + abstract)
                    if s > 0:
                        results.append({
                            "title": title,
                            "abstract": abstract,
                            "link": link,
                            "source": "Google Scholar",
                            "score": s,
                        })
            except Exception as e:
                print(f"Scholar kw error: {e}", file=sys.stderr)
            time.sleep(2)
    except Exception as e:
        print(f"Scholar import error: {e}", file=sys.stderr)
    return results


# -------- Summarize with Claude --------
def summarize(title, abstract):
    if not abstract:
        return "_No abstract available._"
    prompt = f"""Summarize this research paper in 3 concise bullet points:
- Key findings
- Methods used
- Why it matters

Title: {title}
Abstract: {abstract}

Return only the bullets, no preamble."""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"_Summary failed: {e}_"


# -------- Slack --------
def post_slack(papers):
    if not papers:
        text = "📭 No new papers matching your keywords today."
        requests.post(SLACK_WEBHOOK, json={"text": text})
        return

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "📚 Daily Research Digest"}}]
    for p in papers:
        summary = summarize(p["title"], p["abstract"])
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{p['link']}|{p['title']}>*\n_Source: {p['source']}_\n\n{summary}",
            },
        })
        blocks.append({"type": "divider"})
    requests.post(SLACK_WEBHOOK, json={"blocks": blocks})


# -------- Main --------
def main():
    all_papers = fetch_crossref() + fetch_arxiv() + fetch_scholar()
    # dedupe by title
    seen, unique = set(), []
    for p in all_papers:
        key = p["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    top = sorted(unique, key=lambda x: x["score"], reverse=True)[:TOP_N]
    post_slack(top)


if __name__ == "__main__":
    main()
