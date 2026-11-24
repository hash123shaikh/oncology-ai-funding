#!/usr/bin/env python3
"""Monthly updater for the Oncology x AI funding tracker.

Steps
  1. Health-check every agency link in sources.yaml.
  2. Pull live (open / forthcoming) calls from structured APIs:
       - Grants.gov  (US federal: NIH/NCI, NSF, DoD CDMRP, ...)
       - EU Funding & Tenders portal (Horizon Europe, EU Cancer Mission, ...)
  3. Scrape each agency's funding page for links that look like calls,
     dropping ones whose deadline has already passed.
  4. Score relevance to oncology and AI, track first-seen dates, and
     regenerate README.md, data/opportunities.json/.csv and a monthly digest.

Run:  python scripts/update.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.yaml"
STATE = ROOT / "data" / "opportunities.json"
CSV_OUT = ROOT / "data" / "opportunities.csv"
README = ROOT / "README.md"
DIGESTS = ROOT / "digests"

REPO = "hash123shaikh/oncology-ai-funding"
TODAY = dt.date.today()
NOW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
RETENTION_DAYS = 120          # forget items not seen for this long
MAX_PER_SOURCE = 15           # scraped links shown per agency
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/124.0 Safari/537.36 (+https://github.com/{REPO})"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------------------------------------------- relevance
ONCO_RE = re.compile(
    r"cancer|oncolog|tumou?r|carcinom|neoplas|leuka?emia|lymphoma|myeloma|melanoma|"
    r"sarcoma|glioma|metasta|radiotherap|radiation (?:oncology|therapy)|chemotherap|"
    r"immuno-?oncology|mammogra|cervical screening|precancer",
    re.I,
)
AI_RE = re.compile(
    r"artificial intelligence|\bA\.?I\b|machine learning|deep learning|data science|"
    r"computational|informatics|digital health|algorithm|neural net|foundation model|"
    r"large language|\bLLMs?\b|radiomic|imaging|image analysis|predictive model|"
    r"big data|data-driven|bioinformatic|software|compute",
    re.I,
)
HEALTH_RE = re.compile(r"health|medic|clinic|patient|biomed|disease|diagnos|care\b", re.I)


def relevance(text: str) -> tuple[int, str]:
    onco, ai = bool(ONCO_RE.search(text)), bool(AI_RE.search(text))
    if onco and ai:
        return 3, "🎯"
    if onco:
        return 2, "🎗️"
    if ai and HEALTH_RE.search(text):
        return 1, "🤖"
    return 0, ""


# ------------------------------------------------------------------ helpers
session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> tuple[requests.Response | None, str]:
    """GET with one retry and an SSL fallback (many gov sites have bad certs)."""
    for attempt in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            return r, ""
        except requests.exceptions.SSLError:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = session.get(url, timeout=TIMEOUT, verify=False)
                return r, "ssl-warning"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}"
        time.sleep(3)
    return None, err


BLOCK_MARKERS = ("request rejected", "requested url was rejected", "access denied",
                 "attention required", "just a moment", "captcha")


def classify(resp: requests.Response | None, err: str) -> str:
    if resp is None:
        return "error"
    if resp.status_code in (401, 403, 405, 406, 429, 503):
        return "blocked"
    if resp.status_code >= 400:
        return "broken"
    if any(m in resp.text[:5000].lower() for m in BLOCK_MARKERS):
        return "blocked"
    return "ok"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_PATTERNS = [
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s\-/.]+{MONTHS}[\s\-/.,]+\d{{4}}\b",
    rf"\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",
    r"\b\d{1,2}[\-/.]\d{1,2}[\-/.]\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
]
DEADLINE_HINT = re.compile(r"deadline|last date|closing|closes|due|submission|apply by|till|until", re.I)


def find_deadline(text: str) -> dt.date | None:
    """Return the latest date mentioned near a deadline keyword, if any."""
    if not DEADLINE_HINT.search(text):
        return None
    found = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            try:
                d = dateparser.parse(m.group(0), dayfirst=True, fuzzy=True).date()
                if 2000 < d.year < 2100:
                    found.append(d)
            except (ValueError, OverflowError):
                pass
    return max(found) if found else None


def parse_us_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


# ------------------------------------------------------- structured sources
GRANTS_GOV_QUERIES = [
    "cancer artificial intelligence", "cancer machine learning", "cancer imaging",
    "cancer data science", "cancer informatics", "oncology", "radiation oncology",
    "cancer", "tumor", "artificial intelligence health",
]


AGENCY_NAMES = {
    "HHS-NIH": "NIH", "HHS-CDC": "CDC", "HHS-FDA": "FDA", "HHS-AHRQ": "AHRQ",
    "HHS-HRSA": "HRSA", "HHS": "HHS", "NSF": "NSF", "DOD-AMRAA": "DoD CDMRP",
    "DOD": "DoD", "VA": "Veterans Affairs", "DOE": "Dept. of Energy", "DOS": "State Dept.",
    "NASA": "NASA", "USAID": "USAID",
}


def grants_gov() -> list[dict]:
    out: dict[str, dict] = {}
    for q in GRANTS_GOV_QUERIES:
        try:
            r = session.post(
                "https://api.grants.gov/v1/api/search2",
                json={"keyword": q, "oppStatuses": "forecasted|posted", "rows": 250},
                timeout=60,
            )
            hits = r.json()["data"]["oppHits"]
        except Exception as e:  # noqa: BLE001
            print(f"  grants.gov '{q}' failed: {e}", file=sys.stderr)
            continue
        for h in hits:
            title = clean(h.get("title"))
            score, tag = relevance(title)
            if score == 0:
                continue
            close = parse_us_date(h.get("closeDate"))
            if close and close < TODAY:
                continue
            key = f"grantsgov:{h['id']}"
            code = h.get("agencyCode", "")
            agency = next((v for k, v in AGENCY_NAMES.items() if code.startswith(k)), code)
            out[key] = {
                "key": key, "source": "Grants.gov", "section": "grantsgov",
                "title": title, "agency": agency,
                "number": h.get("number", ""),
                "url": f"https://www.grants.gov/search-results-detail/{h['id']}",
                "deadline": close.isoformat() if close else "",
                "status": h.get("oppStatus", ""),
                "score": score, "tag": tag,
            }
        time.sleep(1)
    return list(out.values())


EU_QUERIES = ["cancer", "oncology", "tumour", "artificial intelligence health",
              "cancer imaging", "cancer mission"]


def eu_portal() -> list[dict]:
    out: dict[str, dict] = {}
    query = {"bool": {"must": [
        {"terms": {"type": ["1", "2", "8"]}},                 # calls / topics
        {"terms": {"status": ["31094501", "31094502"]}},      # forthcoming / open
    ]}}
    for q in EU_QUERIES:
        for page in (1, 2):
            try:
                r = session.post(
                    "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
                    params={"apiKey": "SEDIA", "text": q, "pageSize": 50, "pageNumber": page},
                    files={"query": ("blob", json.dumps(query), "application/json"),
                           "languages": ("blob", json.dumps(["en"]), "application/json")},
                    timeout=60,
                )
                results = r.json().get("results", [])
            except Exception as e:  # noqa: BLE001
                print(f"  EU '{q}' failed: {e}", file=sys.stderr)
                break
            for res in results:
                m = res.get("metadata", {})
                ident = (m.get("identifier") or [""])[0]
                title = clean((m.get("title") or [res.get("summary") or ""])[0])
                if not ident or not title:
                    continue
                score, tag = relevance(title)
                if score == 0:
                    continue
                deadlines = []
                for d in m.get("deadlineDate") or []:
                    try:
                        deadlines.append(dateparser.parse(d).date())
                    except (ValueError, OverflowError):
                        pass
                future = [d for d in deadlines if d >= TODAY]
                if deadlines and not future:
                    continue
                prog = (m.get("frameworkProgramme") or [""])[0]
                key = f"eu:{ident}"
                out[key] = {
                    "key": key, "source": "EU Funding & Tenders", "section": "eu",
                    "title": title, "agency": "European Commission",
                    "number": ident,
                    "url": ("https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
                            f"screen/opportunities/topic-details/{ident}"),
                    "deadline": min(future).isoformat() if future else "",
                    "status": {"31094501": "forthcoming", "31094502": "open"}.get(
                        (m.get("status") or [""])[0], ""),
                    "programme": prog, "score": score, "tag": tag,
                }
            if len(results) < 50:
                break
            time.sleep(1)
    return list(out.values())


# ------------------------------------------------------------ web scraping
CALL_RE = re.compile(
    r"\bcall|grant|award|fellowship|funding|\bRFAs?\b|\bRFPs?\b|NOFO|\bFOA|proposal|"
    r"scheme|challenge|opportunit|programme for|prize|scholar|research program|"
    r"invit(?:es|ation)|apply|application",
    re.I,
)
SKIP_RE = re.compile(
    r"result|closed|archive|past |previous|awardee|winners|sanctioned|selected|shortlist|"
    r"recruit|vacanc|career|job|employ|transfer of technology|tender|login|log in|sign in|register|privacy|cookie|"
    r"contact|about us|faq|sitemap|annual report|\brti\b|press release|newsletter|"
    r"donat|give now|fundrais|subscribe|terms|policy on|guidelines?\b|\bguide\b|news|"
    r"decision|publication|recipient|funded|awarded|announced|ceremony|template|"
    r"example|webinar|forms?\b|resources|nominate|find a|highlights|conversation|"
    r"playbook|search tool|childcare|travel grant|accessib|journalism|explore|"
    r"largest|record|history|faqs?\b|how to|tips|statistics|report",
    re.I,
)


def scrape_calls(src: dict, html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    base_host = urlparse(base_url).netloc
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" "))
        if not (12 <= len(text) <= 220):
            continue
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        url, _ = urldefrag(urljoin(base_url, href))
        if url.rstrip("/") == base_url.rstrip("/") or url in seen or text.lower() in seen:
            continue
        if not CALL_RE.search(text) or SKIP_RE.search(text):
            continue
        context = clean(a.parent.get_text(" ") if a.parent else text)[:400]
        deadline = find_deadline(text + " " + context)
        if deadline and deadline < TODAY:
            continue
        seen.update((url, text.lower()))
        score, tag = relevance(text + " " + context)
        items.append({
            "key": f"web:{url}", "source": src["name"], "section": "web",
            "category": src["_category"], "title": text, "agency": src["name"],
            "number": "", "url": url,
            "deadline": deadline.isoformat() if deadline else "",
            "status": "", "score": score, "tag": tag,
            "external": urlparse(url).netloc != base_host,
        })
    items.sort(key=lambda x: -x["score"])
    return items[:MAX_PER_SOURCE]


# ------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"opportunities": {}, "sources": {}, "runs": []}


def merge(state: dict, items: list[dict]) -> list[dict]:
    """Merge this run's items into state; return the items that are new."""
    opps = state["opportunities"]
    first_run = not opps
    new = []
    for it in items:
        prev = opps.get(it["key"])
        it["first_seen"] = prev["first_seen"] if prev else TODAY.isoformat()
        it["last_seen"] = TODAY.isoformat()
        if not prev and not first_run:
            new.append(it)
        opps[it["key"]] = it
    cutoff = (TODAY - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    for k in [k for k, v in opps.items() if v["last_seen"] < cutoff]:
        del opps[k]
    return new


# ------------------------------------------------------------------ render
STATUS_ICON = {"ok": "✅", "blocked": "🔒", "broken": "⚠️", "error": "⚠️"}


def md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def fmt_deadline(d: str) -> str:
    if not d:
        return "—"
    days = (dt.date.fromisoformat(d) - TODAY).days
    flag = " ⏰" if days <= 30 else ""
    return f"{d}{flag}"


def call_table(items: list[dict], limit: int | None = None, show_source=False) -> str:
    items = sorted(items, key=lambda x: (-x["score"], x["deadline"] or "9999"))
    if limit:
        items = items[:limit]
    head = "| Match | Opportunity | " + ("Source | " if show_source else "") + \
           "Agency / ID | Deadline |\n|:-:|---|" + ("---|" if show_source else "") + "---|---|\n"
    rows = []
    for it in items:
        ident = md_escape(" · ".join(x for x in (it.get("agency"), it.get("number")) if x))
        rows.append(
            f"| {it['tag']} | [{md_escape(it['title'])}]({it['url']}) | "
            + (f"{md_escape(it['source'])} | " if show_source else "")
            + f"{ident} | {fmt_deadline(it['deadline'])} |"
        )
    return head + "\n".join(rows) + "\n" if rows else "_No matching open calls found this month._\n"


def render(cfg: dict, state: dict, grants: list, eu: list, web: dict, new: list) -> str:
    st = state["sources"]
    n_sources = sum(len(c["sources"]) for c in cfg["categories"])
    total_live = len(grants) + len(eu) + sum(len(v) for v in web.values())
    out = []
    out.append(f"""# 🎗️🤖 Oncology × AI Research Funding Tracker

[![Monthly update](https://github.com/{REPO}/actions/workflows/monthly-update.yml/badge.svg)](https://github.com/{REPO}/actions/workflows/monthly-update.yml)
![Last updated](https://img.shields.io/badge/last%20updated-{TODAY.isoformat().replace('-', '--')}-blue)
![Agencies](https://img.shields.io/badge/agencies-{n_sources}-green)
![Live calls](https://img.shields.io/badge/live%20calls-{total_live}-orange)

A curated, **automatically updated** list of funding agencies and open calls for
**AI / data-science research in oncology** (radiomics, imaging AI, digital pathology,
clinical AI, cancer informatics), with a focus on opportunities open to
researchers in **India** and internationally.

> 🔄 Refreshed on the **1st of every month** by a GitHub Action
> ([`scripts/update.py`](scripts/update.py)). Last run: **{NOW}**.
> Inspired by the [TMC CRS Research Funding Portal](https://tmc.gov.in/crs_web/CRS-Funding).

**Legend:** 🎯 oncology + AI · 🎗️ oncology · 🤖 AI in health · ⏰ deadline within 30 days ·
✅ site reachable · 🔒 site blocks bots (link still works in a browser) · ⚠️ link problem

## Contents
- [🆕 New this month](#-new-this-month)
- [🇺🇸 Live US federal calls (Grants.gov)](#-live-us-federal-calls-grantsgov)
- [🇪🇺 Live EU calls (Horizon Europe & EU4Health)](#-live-eu-calls-horizon-europe--eu4health)
- [📡 Calls spotted on agency websites](#-calls-spotted-on-agency-websites)
- [🗂️ Agency directory](#️-agency-directory)
- [⚙️ How it works](#️-how-it-works)
""")

    out.append("\n## 🆕 New this month\n\n")
    if not state.get("runs") or len(state["runs"]) <= 1:
        out.append("_First run: everything below is the baseline. From next month, newly "
                   "posted calls will show up here._\n")
    elif new:
        out.append(f"{len(new)} opportunities appeared since the last update "
                   f"(full list in [`digests/{TODAY:%Y-%m}.md`](digests/{TODAY:%Y-%m}.md)).\n\n")
        out.append(call_table(new, limit=40, show_source=True))
    else:
        out.append("_Nothing new since last month._\n")

    out.append("\n## 🇺🇸 Live US federal calls (Grants.gov)\n\n"
               "Open or forecasted NIH / NCI / NSF / DoD opportunities whose titles mention "
               "cancer or AI-in-health. Many NIH NOFOs accept **foreign institutions**; "
               "check each one's eligibility section.\n\n")
    out.append(f"<details open><summary><b>{len(grants)} calls</b>, top 60 by relevance</summary>\n\n")
    out.append(call_table(grants, limit=60))
    out.append("\n</details>\n")

    out.append("\n## 🇪🇺 Live EU calls (Horizon Europe & EU4Health)\n\n"
               "Open or forthcoming topics on the EU Funding & Tenders portal. Indian "
               "institutions can join Horizon Europe consortia (often self-funded; check "
               "each topic).\n\n")
    out.append(f"<details open><summary><b>{len(eu)} topics</b></summary>\n\n")
    out.append(call_table(eu, limit=60))
    out.append("\n</details>\n")

    out.append("\n## 📡 Calls spotted on agency websites\n\n"
               "Links that look like calls, grants or fellowships, scraped from each agency's "
               "funding page. Items with a past deadline are dropped automatically. "
               "**Always confirm details on the agency site.**\n")
    for cat in cfg["categories"]:
        block = []
        for s in cat["sources"]:
            items = web.get(s["name"], [])
            if not items:
                continue
            block.append(f"<details><summary><b>{s['name']}</b> ({len(items)})</summary>\n\n")
            for it in items:
                dl = f" · _deadline {fmt_deadline(it['deadline'])}_" if it["deadline"] else ""
                tag = f"{it['tag']} " if it["tag"] else ""
                block.append(f"- {tag}[{md_escape(it['title'])}]({it['url']}){dl}\n")
            block.append("\n</details>\n")
        if block:
            out.append(f"\n### {cat['title']}\n\n")
            out.extend(block)

    out.append("\n## 🗂️ Agency directory\n")
    for cat in cfg["categories"]:
        out.append(f"\n### {cat['title']}\n\n| | Agency | Focus | Region |\n|:-:|---|---|---|\n")
        for s in cat["sources"]:
            icon = STATUS_ICON.get(st.get(s["name"], {}).get("status", ""), "❔")
            home = f" ([home]({s['home']}))" if s.get("home") else ""
            out.append(f"| {icon} | [{md_escape(s['name'])}]({s['url']}){home} | "
                       f"{md_escape(s.get('focus', ''))} | {s.get('region', '')} |\n")

    out.append(f"""
## ⚙️ How it works

| Step | What happens |
|---|---|
| 1 | [`sources.yaml`](sources.yaml) holds the curated list of agencies (edit it to add more). |
| 2 | On the 1st of each month, [`.github/workflows/monthly-update.yml`](.github/workflows/monthly-update.yml) runs [`scripts/update.py`](scripts/update.py). |
| 3 | The script checks every link, queries the **Grants.gov** and **EU Funding & Tenders** APIs, and scrapes agency pages for call/grant links. |
| 4 | Expired calls are dropped, new ones are flagged, and the README, [`data/opportunities.csv`](data/opportunities.csv) and a monthly digest in [`digests/`](digests/) are committed. |

You can also run it by hand: **Actions → Monthly funding update → Run workflow**, or locally:

```bash
pip install -r requirements.txt
python scripts/update.py
```

### Contributing
Know a funder that's missing? Add it to `sources.yaml` and open a PR. Ideas especially
welcome: state-level Indian schemes, pharma investigator-initiated research (IIR) programmes,
and LMIC-focused oncology funders.

### Disclaimer
This list is compiled automatically and may contain errors or miss calls. Always check
eligibility, budget, and deadlines on the funder's official website before you apply.
""")
    return "".join(out)


def write_digest(new: list[dict]) -> None:
    if not new:
        return
    DIGESTS.mkdir(exist_ok=True)
    p = DIGESTS / f"{TODAY:%Y-%m}.md"
    p.write_text(f"# New funding opportunities: {TODAY:%B %Y}\n\n"
                 f"{len(new)} new items detected on {TODAY.isoformat()}.\n\n"
                 + call_table(new, show_source=True))


def write_csv(state: dict) -> None:
    cols = ["tag", "title", "source", "agency", "number", "deadline", "status",
            "url", "first_seen", "last_seen"]
    rows = sorted(state["opportunities"].values(),
                  key=lambda x: (x["last_seen"] != TODAY.isoformat(), -x["score"], x["title"]))
    with CSV_OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if r["last_seen"] == TODAY.isoformat():
                w.writerow(r)


# -------------------------------------------------------------------- main
def main() -> None:
    cfg = yaml.safe_load(SOURCES.read_text())
    state = load_state()
    web: dict[str, list] = {}

    for cat in cfg["categories"]:
        for s in cat["sources"]:
            s["_category"] = cat["id"]
            resp, err = fetch(s["url"])
            status = classify(resp, err)
            st = {"status": status, "checked": TODAY.isoformat(),
                  "http": resp.status_code if resp is not None else None,
                  "error": err}
            if resp is not None and resp.url.rstrip("/") != s["url"].rstrip("/"):
                st["redirected_to"] = resp.url
            state["sources"][s["name"]] = st
            n = 0
            if status == "ok" and s.get("scrape", True):
                try:
                    web[s["name"]] = scrape_calls(s, resp.text, resp.url)
                    n = len(web[s["name"]])
                except Exception as e:  # noqa: BLE001
                    print(f"  scrape failed: {e}", file=sys.stderr)
            print(f"[{STATUS_ICON[status]} {st['http']}] {s['name']}: {n} links")

    print("Querying Grants.gov ...")
    grants = grants_gov()
    print(f"  {len(grants)} relevant open calls")
    print("Querying EU Funding & Tenders ...")
    eu = eu_portal()
    print(f"  {len(eu)} relevant open topics")

    all_items = grants + eu + [i for v in web.values() for i in v]
    state.setdefault("runs", []).append({"date": TODAY.isoformat(), "items": len(all_items)})
    new = merge(state, all_items)
    print(f"{len(all_items)} live items, {len(new)} new")

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True))
    write_csv(state)
    write_digest(new)
    README.write_text(render(cfg, state, grants, eu, web, new))
    print("README.md updated")


if __name__ == "__main__":
    main()
