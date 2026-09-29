#!/usr/bin/env python3
"""
SDL Weekly - publisher.

Reads an approved draft issue, keeps only the ticked items, and writes:
  issues/<week>.md          the newsletter (paste its rendered view into LinkedIn)
  issues/<week>-teaser.txt  a short LinkedIn post to announce the issue

In GitHub Actions it reads the issue from GITHUB_EVENT_PATH.
Run locally:  python scripts/publish.py --body-file some_draft.md
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
OUT = ROOT / "out"
ISSUES = ROOT / "issues"

SECTION_KEYS = {"papers": "papers", "hardware": "hardware", "software": "software",
                "agent innovation": "agents"}
SECTION_TITLES = {"papers": "Papers", "hardware": "Hardware",
                  "software": "Software", "agents": "Agent innovation"}
PLACEHOLDERS = {"_write your editor's note here._", "_none this week._", "_no candidates this week._"}

ITEM_RE = re.compile(r"^-\s*\[([ xX])\]\s+\*\*([A-Za-z]+\d+)\*\*\s+\[(.+?)\]\((\S+?)\)\s*$")
SUB_RE = re.compile(r"<!--\s*sub:(\d+)\s*-->")


def strip_comments(s: str) -> str:
    return re.sub(r"<!--.*?-->", "", s, flags=re.S)


def free_text(lines: list[str]) -> str:
    text = strip_comments("\n".join(lines)).strip()
    return "" if text.lower() in PLACEHOLDERS else text


def parse(body: str) -> dict:
    body = body.replace("\r\n", "\n")
    header = re.search(r"sdl-weekly-draft week=(\S+) since=(\S+) until=(\S+)", body)
    doc = {
        "week": header.group(1) if header else dt.date.today().strftime("%G-W%V"),
        "since": header.group(2) if header else "",
        "until": header.group(3) if header else "",
        "editor": "", "pick": "", "events": "",
        "items": [],
    }
    section, buf, current = None, [], None
    free_sections = {"editor's note": "editor", "editor’s note": "editor",
                     "events and calls": "events", "paper of the week": "pick"}

    def flush():
        if section in ("editor", "events"):
            doc[section] = free_text(buf)
        elif section == "pick":
            m = re.search(r"Pick:\s*([A-Za-z]+\d+)", strip_comments("\n".join(buf)))
            doc["pick"] = m.group(1).upper() if m else ""

    for line in body.split("\n"):
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            flush()
            name = h.group(1).strip().lower()
            section = free_sections.get(name) or SECTION_KEYS.get(name)
            buf, current = [], None
            continue
        if section in ("editor", "events", "pick"):
            buf.append(line)
            continue
        if section not in SECTION_KEYS.values():
            continue
        m = ITEM_RE.match(line.strip())
        if m:
            current = {"checked": m.group(1).lower() == "x", "id": m.group(2).upper(),
                       "title": m.group(3).strip(), "url": m.group(4).strip(),
                       "section": section, "meta": "", "note": "", "submission": None}
            doc["items"].append(current)
            continue
        if current is None or not line.startswith(" "):
            continue
        sub = line.strip()
        if sub.startswith("- "):
            sub = sub[2:].strip()
            low = sub.lower()
            if low.startswith("why it matters:"):
                current["note"] = sub.split(":", 1)[1].strip()
                current["_in_note"] = True
                continue
            current["_in_note"] = False
            if low.startswith("abstract:"):
                continue
            if not current["meta"]:
                sm = SUB_RE.search(sub)
                if sm:
                    current["submission"] = int(sm.group(1))
                current["meta"] = strip_comments(sub).strip()
        elif current.get("_in_note") and sub:
            current["note"] = (current["note"] + " " + sub).strip()  # multi-line note
    flush()
    return doc


def meta_parts(meta: str) -> tuple[str, str]:
    """'A et al. · Journal · 2026-09-24' → ('A et al.', 'Journal')"""
    parts = [p.strip() for p in meta.split("·")]
    if parts and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-1]):
        parts = parts[:-1]
    authors = parts[0] if parts else ""
    source = parts[1] if len(parts) > 1 else ""
    return authors, source


def week_range(since: str, until: str) -> str:
    try:
        a, b = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    except ValueError:
        return ""
    if a.month == b.month:
        return f"{a.day}–{b.day} {b.strftime('%B %Y')}"
    return f"{a.day} {a.strftime('%B')} – {b.day} {b.strftime('%B %Y')}"


def item_line(it: dict) -> str:
    authors, source = meta_parts(it["meta"])
    extra = " · ".join(x for x in [authors, f"*{source}*" if source else ""] if x)
    line = f"- **[{it['title']}]({it['url']})**"
    if extra:
        line += f" · {extra}"
    if it["note"]:
        line += f"  \n  {it['note']}"
    return line


def build(doc: dict, issue_no: int, repo: str) -> tuple[str, str]:
    name = CFG.get("newsletter_name", "SDL Weekly")
    chosen = [i for i in doc["items"] if i["checked"]]
    featured = next((i for i in chosen if i["id"] == doc["pick"]), None)
    if featured is None and doc["pick"]:
        featured = next((i for i in doc["items"] if i["id"] == doc["pick"]), None)
    rng = week_range(doc["since"], doc["until"])

    md = [f"# {name}: Issue {issue_no}"]
    if rng:
        md.append(f"*Week of {rng}*")
    md.append("")
    if doc["editor"]:
        md += [doc["editor"], ""]
    if featured:
        authors, source = meta_parts(featured["meta"])
        md += ["## Paper of the week", f"**[{featured['title']}]({featured['url']})**  "]
        md.append(" · ".join(x for x in [authors, f"*{source}*" if source else ""] if x))
        md.append("")
        if featured["note"]:
            md += [featured["note"], ""]
    counts = {}
    for key in ("papers", "hardware", "software", "agents"):
        sec = [i for i in chosen if i["section"] == key and i is not featured]
        counts[key] = len(sec)
        if sec:
            md += [f"## {SECTION_TITLES[key]}"] + [item_line(i) for i in sec] + [""]
    if doc["events"]:
        md += ["## Events and calls", doc["events"], ""]

    footer = ["---", f"*Curated by {CFG.get('editor_name', '')}.*"]
    if repo:
        footer.append(f"*Past issues: https://github.com/{repo}/tree/HEAD/issues · "
                      f"Share your SDL work for a future issue: https://github.com/{repo}/issues/new/choose*")
    md += footer

    # LinkedIn teaser post (plain text: LinkedIn posts don't render markdown)
    t = [f"New issue of {name} is out: Issue {issue_no}" + (f", week of {rng}." if rng else "."), ""]
    if featured:
        a, _ = meta_parts(featured["meta"])
        t += [f"Paper of the week: {featured['title']}" + (f" ({a})" if a else ""), ""]
    others = [i for i in chosen if i is not featured][:3]
    if others:
        t.append("Also inside:")
        t += [f"• {i['title']}" for i in others]
        t.append("")
    unit = {"papers": ("paper", "papers"), "hardware": ("hardware item", "hardware items"),
            "software": ("software item", "software items"), "agents": ("agent paper", "agent items")}
    summary = ", ".join(f"{n} {unit[k][0] if n == 1 else unit[k][1]}" for k, n in counts.items() if n)
    if summary:
        t += [f"This week: {summary}.", ""]
    t.append(f"Read and subscribe: {CFG.get('linkedin_newsletter_url') or '<your LinkedIn newsletter link>'}")
    t += ["", "#SelfDrivingLabs #LabAutomation #MaterialsScience #Chemistry #AIforScience"]
    return "\n".join(md) + "\n", "\n".join(t) + "\n"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ISSUES.mkdir(exist_ok=True)
    if "--body-file" in sys.argv:
        body = Path(sys.argv[sys.argv.index("--body-file") + 1]).read_text(encoding="utf-8")
    else:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        body = event["issue"]["body"] or ""
    doc = parse(body)
    chosen = [i for i in doc["items"] if i["checked"]]
    if not chosen and not doc["editor"]:
        (OUT / "error.txt").write_text(
            "Nothing to publish: no items are ticked and the editor's note is empty. "
            "Tick some items, then remove and re-add the `approved` label.", encoding="utf-8")
        sys.exit(1)

    existing = sorted(p for p in ISSUES.glob("*.md") if not p.name.endswith("-teaser.md"))
    target = ISSUES / f"{doc['week']}.md"
    issue_no = len([p for p in existing if p != target]) + 1
    md, teaser = build(doc, issue_no, os.environ.get("GITHUB_REPOSITORY", ""))
    target.write_text(md, encoding="utf-8")
    (ISSUES / f"{doc['week']}-teaser.txt").write_text(teaser, encoding="utf-8")

    subs = sorted({i["submission"] for i in chosen if i["submission"]})
    (OUT / "week.txt").write_text(doc["week"], encoding="utf-8")
    (OUT / "published_path.txt").write_text(f"issues/{doc['week']}.md", encoding="utf-8")
    (OUT / "submissions.txt").write_text("\n".join(map(str, subs)) + ("\n" if subs else ""), encoding="utf-8")
    print(f"Published {target.name}: {len(chosen)} items, featured={doc['pick'] or 'none'}, submissions={subs}")


if __name__ == "__main__":
    main()
