#!/usr/bin/env python3
"""
SDL Weekly - collector.

Pulls the past week's candidate items from arXiv, ChemRxiv, OpenAlex, GitHub
and community submissions, filters them for relevance, sorts them into the
newsletter sections, and writes a draft GitHub issue body to out/draft.md.

Run locally:   python scripts/collect.py --dry-run
(--dry-run does not update data/seen.json)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
OUT = ROOT / "out"
SEEN_PATH = ROOT / "data" / "seen.json"

TODAY = dt.date.today()
SINCE = TODAY - dt.timedelta(days=int(CFG.get("lookback_days", 7)))
_ref = TODAY - dt.timedelta(days=1)
WEEK_LABEL = f"{_ref.isocalendar()[0]}-W{_ref.isocalendar()[1]:02d}"

SECTIONS = ["papers", "hardware", "software", "agents"]
SECTION_TITLES = {"papers": "Papers", "hardware": "Hardware",
                  "software": "Software", "agents": "Agent innovation"}
SECTION_PREFIX = {"papers": "P", "hardware": "H", "software": "S", "agents": "A"}
MAX_BODY = 60000  # GitHub issue bodies are limited to 65,536 characters


# ───────────────────────── helpers ─────────────────────────

def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def http_get(url: str, headers: dict | None = None, retries: int = 3) -> str:
    h = {"User-Agent": f"SDL-Weekly-Newsletter/1.0 (mailto:{CFG.get('contact_email', '')})"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 422):
                raise
            last = e
        except Exception as e:  # network hiccup, timeout ...
            last = e
        time.sleep(3 * (attempt + 1))
    raise last  # type: ignore[misc]


def clean(s: str | None) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)          # strip tags (JATS abstracts etc.)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())[:120]


def item_keys(it: dict) -> list[str]:
    keys = ["t:" + norm_title(it["title"])]
    if it.get("doi"):
        keys.append("d:" + it["doi"].lower().replace("https://doi.org/", ""))
    if it.get("url"):
        keys.append("u:" + it["url"].lower().rstrip("/"))
    return keys


def has_word(text: str, word: str) -> bool:
    """Whole-word match that also accepts a plural 's' ('agent' matches 'agents', not 'reagent')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"s?(?![a-z0-9])", text) is not None


def has_fragment(text: str, frag: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(frag.lower()), text) is not None


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def src_cfg(name: str) -> dict:
    return (CFG.get("sources") or {}).get(name) or {}


# ───────────────────────── sources ─────────────────────────

ATOM = {"a": "http://www.w3.org/2005/Atom"}
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def fetch_arxiv() -> list[dict]:
    c = src_cfg("arxiv")
    if not c.get("enabled", True):
        return []
    cats = " OR ".join(f"cat:{x}" for x in c.get("categories", []))
    terms = " OR ".join(f'abs:"{t}"' for t in CFG["search_terms"])
    query = f"({cats}) AND ({terms})" if cats else terms
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": c.get("max_results", 200)})
    root = ET.fromstring(http_get(url))
    items = []
    for e in root.findall("a:entry", ATOM):
        published = (e.findtext("a:published", "", ATOM) or "")[:10]
        if not published or dt.date.fromisoformat(published) < SINCE:
            continue
        link = re.sub(r"v\d+$", "", (e.findtext("a:id", "", ATOM) or "").strip())
        items.append({
            "title": clean(e.findtext("a:title", "", ATOM)),
            "url": link.replace("http://", "https://"),
            "authors": [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            "source": "arXiv",
            "date": published,
            "abstract": clean(e.findtext("a:summary", "", ATOM)),
            "doi": e.findtext(f"{ARXIV_NS}doi") or "",
        })
    return items


def fetch_chemrxiv() -> list[dict]:
    if not src_cfg("chemrxiv").get("enabled", True):
        return []
    base = "https://chemrxiv.org/engage/chemrxiv/public-api/v1/items"
    items, failures = [], 0
    for term in CFG["search_terms"]:
        params = {"term": term, "searchDateFrom": f"{SINCE.isoformat()}T00:00:00.000Z",
                  "limit": 50, "sort": "PUBLISHED_DATE_DESC"}
        try:
            data = json.loads(http_get(base + "?" + urllib.parse.urlencode(params)))
        except Exception as e:
            failures += 1
            log(f"ChemRxiv '{term}': {e}")
            if failures >= 3:
                raise RuntimeError(f"ChemRxiv unavailable ({e})")
            continue
        for hit in data.get("itemHits", []):
            it = hit.get("item", {})
            date = (it.get("publishedDate") or "")[:10]
            if not date or dt.date.fromisoformat(date) < SINCE:
                continue
            items.append({
                "title": clean(it.get("title")),
                "url": f"https://chemrxiv.org/engage/chemrxiv/article-details/{it.get('id')}",
                "authors": [f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                            for a in it.get("authors", [])],
                "source": "ChemRxiv",
                "date": date,
                "abstract": clean(it.get("abstract")),
                "doi": it.get("doi") or "",
            })
        time.sleep(1)
    return items


def _inverted_to_text(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = [(i, w) for w, idxs in inv.items() for i in idxs]
    return " ".join(w for _, w in sorted(pos))


def fetch_openalex() -> list[dict]:
    if not src_cfg("openalex").get("enabled", True):
        return []
    items, failures = [], 0
    for term in CFG["search_terms"]:
        params = {
            "search": f'"{term}"',
            "filter": f"from_publication_date:{SINCE.isoformat()},to_publication_date:{TODAY.isoformat()}",
            "per-page": 50,
            "select": "id,doi,display_name,publication_date,primary_location,authorships,abstract_inverted_index",
            "mailto": CFG.get("contact_email", ""),
        }
        try:
            data = json.loads(http_get("https://api.openalex.org/works?" + urllib.parse.urlencode(params)))
        except Exception as e:
            failures += 1
            log(f"OpenAlex '{term}': {e}")
            if failures >= 3:
                raise RuntimeError(f"OpenAlex unavailable ({e})")
            continue
        for w in data.get("results", []):
            loc = w.get("primary_location") or {}
            source = ((loc.get("source") or {}).get("display_name")) or "Journal"
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            url = w.get("doi") or loc.get("landing_page_url") or w.get("id")
            items.append({
                "title": clean(w.get("display_name")),
                "url": url,
                "authors": [((a.get("author") or {}).get("display_name") or "")
                            for a in (w.get("authorships") or [])],
                "source": source,
                "date": w.get("publication_date") or "",
                "abstract": clean(_inverted_to_text(w.get("abstract_inverted_index"))),
                "doi": doi,
            })
        time.sleep(0.2)
    return items


def fetch_github() -> list[dict]:
    c = src_cfg("github")
    if not c.get("enabled", True):
        return []
    items = []
    for q in c.get("repo_queries", []):
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": f"{q} created:>={SINCE.isoformat()}", "sort": "stars", "order": "desc", "per_page": 20})
        try:
            data = json.loads(http_get(url, gh_headers()))
        except Exception as e:
            log(f"GitHub search '{q}': {e}")
            continue
        for r in data.get("items", []):
            items.append({
                "title": r["full_name"],
                "url": r["html_url"],
                "authors": [r["owner"]["login"]],
                "source": "GitHub, new repository",
                "date": r["created_at"][:10],
                "abstract": r.get("description") or "",
                "topics": r.get("topics") or [],
                "kind": "software",
                "trusted": True,
            })
        time.sleep(2)  # search API is rate limited
    for kind, key in (("software", "watch_software"), ("hardware", "watch_hardware")):
        for full in c.get(key, []) or []:
            try:
                rels = json.loads(http_get(f"https://api.github.com/repos/{full}/releases?per_page=5", gh_headers()))
            except Exception as e:
                log(f"GitHub releases {full}: {e}")
                continue
            for rel in rels:
                date = (rel.get("published_at") or "")[:10]
                if not date or rel.get("draft") or rel.get("prerelease"):
                    continue
                if dt.date.fromisoformat(date) < SINCE:
                    continue
                name = rel.get("name") or rel.get("tag_name")
                items.append({
                    "title": f"{full} {name}",
                    "url": rel["html_url"],
                    "authors": [full.split("/")[0]],
                    "source": "GitHub release",
                    "date": date,
                    "abstract": clean(rel.get("body"))[:400],
                    "kind": kind,
                    "trusted": True,
                })
    return items


def _form_field(body: str, label: str) -> str:
    m = re.search(r"###\s*" + re.escape(label) + r"\s*\n(.*?)(?=\n###\s|\Z)", body, re.S | re.I)
    val = (m.group(1).strip() if m else "")
    return "" if val in ("_No response_", "None") else val


def fetch_submissions() -> list[dict]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return []
    data = json.loads(http_get(
        f"https://api.github.com/repos/{repo}/issues?labels=submission&state=open&per_page=50", gh_headers()))
    section_map = {"paper": "papers", "hardware": "hardware", "software": "software",
                   "agent innovation": "agents"}
    items = []
    for iss in data:
        if "pull_request" in iss:
            continue
        body = (iss.get("body") or "").replace("\r\n", "\n")
        link = _form_field(body, "Link")
        items.append({
            "title": re.sub(r"^\[?submission\]?\s*:?\s*", "", iss["title"], flags=re.I) or "Community submission",
            "url": link or iss["html_url"],
            "authors": [f"submitted by @{iss['user']['login']}"],
            "source": f"Community submission #{iss['number']}",
            "date": iss["created_at"][:10],
            "abstract": _form_field(body, "Short description"),
            "kind": section_map.get(_form_field(body, "Category").lower(), "papers"),
            "submission": iss["number"],
            "trusted": True,
        })
    return items


# ───────────────────────── filtering & sorting ─────────────────────────

def text_of(it: dict) -> str:
    return f"{it['title']} {it.get('abstract', '')} {' '.join(it.get('topics', []))}".lower()


def is_excluded(it: dict) -> bool:
    t = text_of(it)
    return any(has_fragment(t, k) for k in CFG.get("exclude_keywords", []))


def is_relevant(it: dict) -> bool:
    t = text_of(it)
    return any(has_fragment(t, k) for k in CFG.get("must_match_any", []))


def classify(it: dict) -> str:
    title = it["title"].lower()
    abstract = (it.get("abstract") or "").lower()
    kws = CFG.get("section_keywords", {})
    # Agents win even for GitHub items (an LLM lab-agent repo belongs in Agents).
    for sec in ("agents", "hardware", "software"):
        words = kws.get(sec, [])
        in_title = any(has_word(title, w) for w in words)
        in_abs = sum(1 for w in words if has_word(abstract, w))
        if in_title or in_abs >= 2:
            if it.get("kind") and sec != "agents":
                break
            return sec
    return it.get("kind") or "papers"


def score(it: dict) -> int:
    t = text_of(it)
    s = 3 * sum(1 for k in CFG.get("must_match_any", []) if has_fragment(t, k))
    s += sum(1 for k in CFG.get("domain_keywords", []) if has_word(t, k))
    src = it.get("source", "").lower()
    if any(src == j.lower() for j in CFG.get("priority_journals", [])):
        s += 4
    return s


def build_sections(items: list[dict], seen: dict) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {s: [] for s in SECTIONS}
    used: set[str] = set()
    for it in items:
        if not it.get("title") or not it.get("url"):
            continue
        keys = item_keys(it)
        if any(k in used for k in keys):
            continue
        if not it.get("submission"):
            if any(k in seen for k in keys) or is_excluded(it):
                continue
            if not it.get("trusted") and not is_relevant(it):
                continue
        used.update(keys)
        it["section"] = classify(it)
        it["score"] = score(it)
        buckets[it["section"]].append(it)
    cap = int(CFG.get("max_per_section", 20))
    for s in SECTIONS:
        subs = [i for i in buckets[s] if i.get("submission")]
        rest = sorted((i for i in buckets[s] if not i.get("submission")),
                      key=lambda i: (i["score"], i.get("date", "")), reverse=True)
        buckets[s] = subs + rest[:cap]
        for n, it in enumerate(buckets[s], 1):
            it["id"] = f"{SECTION_PREFIX[s]}{n}"
    return buckets


# ───────────────────────── rendering ─────────────────────────

def md_title(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("[", "(").replace("]", ")")).strip()


def safe_url(u: str) -> str:
    return u.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def fmt_authors(a: list[str]) -> str:
    a = [x for x in a if x]
    if not a:
        return ""
    if len(a) <= 2:
        return ", ".join(a)
    return f"{a[0]} et al."


def render_item(it: dict, with_abstract: bool) -> str:
    meta = " · ".join(x for x in [fmt_authors(it.get("authors", [])), it.get("source", ""), it.get("date", "")] if x)
    if it.get("submission"):
        meta += f" <!-- sub:{it['submission']} -->"
    lines = [f"- [ ] **{it['id']}** [{md_title(it['title'])}]({safe_url(it['url'])})",
             f"  - {meta}",
             "  - Why it matters: "]
    if with_abstract and it.get("abstract"):
        ab = it["abstract"]
        ab = ab[:220].rsplit(" ", 1)[0] + "…" if len(ab) > 220 else ab
        lines.append(f"  - Abstract: _{md_title(ab)}_")
    return "\n".join(lines)


def render(buckets: dict, stats: dict, errors: list[str], with_abstract: bool = True) -> str:
    until = TODAY - dt.timedelta(days=1)
    total = sum(len(v) for v in buckets.values())
    stat_line = ", ".join(f"{k} {v}" for k, v in stats.items())
    parts = [
        f"<!-- sdl-weekly-draft week={WEEK_LABEL} since={SINCE.isoformat()} until={until.isoformat()} -->",
        f"# SDL Weekly: {WEEK_LABEL} draft",
        "",
        "**How to curate this draft**",
        "1. Tick the box next to every item you want in the newsletter (you can tick directly on this page).",
        "2. Open the `···` menu at the top of this issue → **Edit**, and write one line after `Why it matters:` "
        "for each ticked item. Ticked items without a note are still included, with just the link.",
        "3. Fill in the editor's note, choose the paper of the week, and add any events.",
        "4. To add something you found yourself, copy an item block into the right section, give it a new ID "
        "(for example `P99`) and tick it.",
        "5. Add the label **approved**. The newsletter file is generated automatically and this issue closes.",
        "",
        f"_{total} candidates from {SINCE.isoformat()} to {until.isoformat()}. Raw results per source: {stat_line}._",
    ]
    if errors:
        parts.append("")
        parts.append("> [!WARNING]\n> Some sources failed this week and may be missing: " + "; ".join(errors))
    parts += [
        "",
        "## Editor's note",
        "<!-- Replace the line below with your intro: the big theme of the week, a trend you noticed, or a personal take. -->",
        "_Write your editor's note here._",
        "",
        "## Paper of the week",
        "<!-- Write the ID of one item after 'Pick:' (for example P3). Its 'Why it matters' note becomes the "
        "feature text, so write a few sentences for it. -->",
        "Pick: ",
        "",
    ]
    for s in SECTIONS:
        parts.append(f"## {SECTION_TITLES[s]}")
        if buckets[s]:
            parts.extend(render_item(it, with_abstract) for it in buckets[s])
        else:
            parts.append("_No candidates this week._")
        parts.append("")
    parts += [
        "## Events and calls",
        "<!-- Optional: conferences, workshops, deadlines, open positions. Use normal markdown bullets. -->",
        "_None this week._",
        "",
    ]
    return "\n".join(parts)


# ───────────────────────── main ─────────────────────────

def load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen: dict, buckets: dict) -> None:
    today = TODAY.isoformat()
    for items in buckets.values():
        for it in items:
            if not it.get("submission"):
                for k in item_keys(it):
                    seen[k] = today
    cutoff = (TODAY - dt.timedelta(days=180)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.parent.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")


FETCHERS = [
    ("Submissions", fetch_submissions),  # first, so a submission wins over duplicates
    ("arXiv", fetch_arxiv),
    ("ChemRxiv", fetch_chemrxiv),
    ("OpenAlex", fetch_openalex),
    ("GitHub", fetch_github),
]


def main() -> None:
    dry = "--dry-run" in sys.argv
    OUT.mkdir(exist_ok=True)
    seen = load_seen()
    stats, errors, all_items = {}, [], []
    for name, fn in FETCHERS:
        try:
            got = fn()
        except Exception as e:
            errors.append(f"{name} ({type(e).__name__})")
            log(f"{name} failed: {e}")
            got = []
        stats[name] = len(got)
        all_items.extend(got)
        log(f"{name}: {len(got)} raw items")

    buckets = build_sections(all_items, seen)
    body = render(buckets, stats, errors)
    if len(body) > MAX_BODY:
        body = render(buckets, stats, errors, with_abstract=False)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n_(Draft truncated: lower max_per_section in config.yml.)_\n"

    (OUT / "draft.md").write_text(body, encoding="utf-8")
    (OUT / "title.txt").write_text(f"SDL Weekly: {WEEK_LABEL} draft", encoding="utf-8")
    if not dry:
        save_seen(seen, buckets)
    log(f"Draft written: {sum(len(v) for v in buckets.values())} candidates, {len(body)} characters")


if __name__ == "__main__":
    main()
