import os
import re
import sys
import yaml
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

# -------- Load config --------
with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

SLOT1_KW = [k.lower() for k in CFG["slot1_title_keywords"]]
SLOT2_KW = [k.lower() for k in CFG["slot2_title_keywords"]]
SLOT3_KW = [k.lower() for k in CFG["slot3_title_keywords"]]
SLOT3_JOURNALS = [j for j in CFG["slot3_journals"]]
PLANT_KW = [k.lower() for k in CFG["plant_keywords"]]
JOURNALS = CFG["journals_crossref"]
LOOKBACK = CFG.get("lookback_hours", 72)

SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
SEEN_FILE = "seen.json"


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
def title_matches(title, kw_list):
    t = (title or "").lower()
    return [kw for kw in kw_list if kw in t]


def has_plant_context(title, abstract):
    blob = f"{title} {abstract}".lower()
    return any(kw in blob for kw in PLANT_KW)


# -------- Crossref fetcher --------
def fetch_crossref_for_journals(journal_list):
    """Fetch recent papers from given journals."""
    results = []
    from_date = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK)).strftime("%Y-%m-%d")
    for journal in journal_list:
        url = (
            f"https://api.crossref.org/works?query.container-title={quote_plus(journal)}"
            f"&filter=from-pub-date:{from_date}&rows=25&sort=published&order=desc"
        )
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "paper-bot/1.0"})
            items = r.json().get("message", {}).get("items", [])
            for it in items:
                title = " ".join(it.get("title", [""]))
                abstract = re.sub(r"<[^>]+>", "", it.get("abstract", "") or "")
                doi = it.get("DOI", "")
                link = f"https://doi.org/{doi}" if doi else it.get("URL", "")
                # verify the journal name in results matches what we queried
                # (Crossref sometimes returns wrong matches)
                containers = [c.lower() for c in it.get("container-title", [])]
                if journal.lower() not in containers:
                    continue
                results.append({
                    "title": title,
                    "abstract": abstract,
                    "link": link,
                    "source": journal,
                })
        except Exception as e:
            print(f"Crossref error for {journal}: {e}", file=sys.stderr)
        time.sleep(0.5)
    return results


# -------- Slot selection --------
def pick_slot(papers, title_kw, require_plant_context=False, allowed_journals=None, used_ids=None):
    """Pick the best paper matching title keywords; optionally restrict to journals + plant context."""
    used_ids = used_ids or set()
    candidates = []
    for p in papers:
        if paper_id(p) in used_ids:
            continue
        if allowed_journals and p["source"] not in allowed_journals:
            continue
        hits = title_matches(p["title"], title_kw)
        if not hits:
            continue
        if require_plant_context and not has_plant_context(p["title"], p["abstract"]):
            continue
        p["_score"] = len(hits)
        candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x["_score"])


# -------- Slack --------
def post_slack(papers):
    if not papers:
        requests.post(SLACK_WEBHOOK, json={"text": "📭 No new papers matching your slot criteria today."})
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

    # Fetch from all relevant journals (slots 1&2 + slot 3 journals)
    all_journals = list({*JOURNALS, *SLOT3_JOURNALS})
    papers = fetch_crossref_for_journals(all_journals)

    # Remove duplicates and already-seen
    run_seen, unique = set(), []
    for p in papers:
        pid = paper_id(p)
        if pid in run_seen or pid in seen:
            continue
        run_seen.add(pid)
        unique.append(p)

    # Pick slots
    picked = []
    used = set()

    s1 = pick_slot(unique, SLOT1_KW, used_ids=used)
    if s1:
        picked.append(s1)
        used.add(paper_id(s1))

    s2 = pick_slot(unique, SLOT2_KW, used_ids=used)
    if s2:
        picked.append(s2)
        used.add(paper_id(s2))

    s3 = pick_slot(unique, SLOT3_KW,
                   require_plant_context=True,
                   allowed_journals=SLOT3_JOURNALS,
                   used_ids=used)
    if s3:
        picked.append(s3)
        used.add(paper_id(s3))

    post_slack(picked)
    for p in picked:
        seen.add(paper_id(p))
    save_seen(seen)


if __name__ == "__main__":
    main()
