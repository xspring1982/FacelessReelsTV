#!/usr/bin/env python3
"""Regenerate the niche list from public pages on facelessreels.tv.

Reads only public, indexable pages (sitemap.xml, /niches, /radar/youtube).
No database, no API keys, no logins. Standard library only.

    python3 scripts/update.py           # fetch, write data/ and README sections
    python3 scripts/update.py --check   # fetch and print, write nothing

MIT License, see LICENSE-CODE.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

SITE = "https://facelessreels.tv"
ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = (
    "FacelessReelsTV-niches/1.0 "
    "(+https://github.com/xspring1982/FacelessReelsTV)"
)

# Rows shown in the README tables (the data files keep every row).
NEW_CHANNEL_ROWS = 8
VIEWS_PER_SUB_ROWS = 5

# Never list political topics about China, whatever Radar picks up.
BLOCKED_TERMS = re.compile(
    r"\b(china|chinese|ccp|beijing|taiwan|tibet|xinjiang|uyghur|hong kong|"
    r"tiananmen|mao|xi jinping|communist party)\b",
    re.I,
)


def fetch(path: str) -> str:
    req = urllib.request.Request(SITE + path, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            break
        except OSError as err:  # network hiccup: wait and retry
            if attempt == 2:
                sys.exit(f"{path}: {err}")
            time.sleep(10 * (attempt + 1))
    time.sleep(1)  # be polite: one request per second
    if "<title>Just a moment" in body:
        sys.exit(f"{path}: the site served a bot check; try again later.")
    return body


def text_lines(page: str) -> list[str]:
    page = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    page = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [ln.strip() for ln in page.split("\n") if ln.strip()]


def number(value: str) -> int | None:
    """'57.4K' -> 57400, '14.4M' -> 14400000, '—' -> None."""
    m = re.fullmatch(r"([\d.,]+)\s*([KMB]?)", value.replace(" views", "").strip())
    if not m:
        return None
    scale = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2)]
    return round(float(m.group(1).replace(",", "")) * scale)


def blocked(*values: str) -> bool:
    return any(BLOCKED_TERMS.search(v or "") for v in values)


# ---------------------------------------------------------------- niches


def published_niche_urls() -> set[str]:
    xml = fetch("/sitemap.xml")
    return {
        u.rstrip("/")
        for u in re.findall(r"<loc>([^<]+)</loc>", xml)
        if re.fullmatch(rf"{re.escape(SITE)}/niches/[a-z0-9-]+/?", u)
    }


def read_niches(published: set[str]) -> list[dict]:
    page = fetch("/niches")

    # Directory categories: name + link (a page once it is published,
    # otherwise an anchor on /niches).
    groups: dict[str, str | None] = {}
    for chunk in page.split('class="ncat ')[1:]:
        chunk = chunk.split("</article>")[0]
        lines = text_lines("<x " + chunk)
        idx = next(i for i, ln in enumerate(lines) if re.fullmatch(r"\d+ niches?", ln))
        href = re.search(r'ncat-open[^>]*href="([^"]+)"', chunk)
        url = SITE + href.group(1) if href and href.group(1).startswith("/") else None
        groups[lines[idx + 1]] = url if url in published else None

    # Niche cards, in page order, under their category headings.
    links = {}
    for block in re.findall(r'"@type":"ItemList".*?\]', page, flags=re.S):
        for name, url in re.findall(r'"name":"([^"]+)","url":"([^"]+)"', block):
            links[name] = url.rstrip("/")

    niches, current = [], None
    lines = text_lines(page)
    for i, ln in enumerate(lines):
        if ln in groups and i + 1 < len(lines) and not lines[i + 1].startswith("from "):
            current = ln
        if re.fullmatch(r"Niche \d+", ln):
            name, meta, desc = lines[i + 1 : i + 4]
            parts = [p.strip() for p in meta.split("·")]
            url = links.get(name)
            if not url or url not in published or blocked(name, desc):
                continue
            niches.append(
                {
                    "niche": name,
                    "category": current,
                    "category_url": groups.get(current),
                    "style": parts[1] if len(parts) > 1 else None,
                    "format": parts[2] if len(parts) > 2 else None,
                    "description": desc,
                    "page_url": url,
                }
            )
    if not niches:
        sys.exit("/niches: no niche cards found; the page layout may have changed.")

    # "Which faceless channels already work in these niches?": the site's own
    # pick of one real channel per niche (most viewed video in that niche).
    examples = {}
    for i, ln in enumerate(lines):
        m = re.fullmatch(r"(.+) · ([\d.,]+[KMB]?) subscribers · top video ([\d.,]+[KMB]?) views", ln)
        if m and i > 0:
            examples[m.group(1)] = (lines[i - 1], number(m.group(2)), number(m.group(3)))
    for n in niches:
        channel, subs, views = examples.get(n["niche"], (None, None, None))
        if channel and blocked(channel):
            channel, subs, views = None, None, None
        n["example_channel"] = channel
        n["example_subscribers"] = subs
        n["example_top_video_views"] = views
    return niches


# ----------------------------------------------------------------- radar


def read_radar() -> dict:
    lines = text_lines(fetch("/radar/youtube"))
    updated = next(ln for ln in lines if ln.startswith("Updated "))
    snapshot = dt.datetime.strptime(updated[len("Updated ") :], "%d %B %Y").date()

    # This week's top breakout videos: "<views>", "<title>", "#<rank>",
    # "<channel> · <style> · <age>", "<ratio>× views/sub".
    videos, seen = [], set()
    for i, ln in enumerate(lines):
        if not re.fullmatch(r"#\d+", ln) or ln in seen or i < 2:
            continue
        ratio = lines[i + 2] if "views/sub" in lines[i + 2] else ""
        meta = [p.strip() for p in lines[i + 1].split("·")]
        if len(meta) < 3 or not lines[i - 2].endswith("views"):
            continue
        seen.add(ln)
        title, channel = lines[i - 1], meta[0]
        if blocked(title, channel):
            continue
        videos.append(
            {
                "rank": int(ln[1:]),
                "title": title,
                "channel": channel,
                "style": meta[1],
                "views": number(lines[i - 2]),
                "views_per_sub": float(ratio.split("×")[0]) if ratio else None,
            }
        )
    videos.sort(key=lambda v: v["rank"])

    # New breakout channels: rank, name, format, style, language code,
    # "Language: …", "N days ago", subs, channel views, top video views.
    channels = []
    start = lines.index("Channel views") + 2
    i = start
    while i + 9 < len(lines) and lines[i].isdigit():
        row = lines[i : i + 10]
        if not row[6].endswith("days ago"):
            break
        if not blocked(row[1]):
            channels.append(
                {
                    "channel": row[1],
                    "format": row[2],
                    "style": row[3],
                    "language": row[5].replace("Language: ", ""),
                    "channel_age_days": int(row[6].split()[0]),
                    "subscribers": number(row[7]),
                    "channel_views": number(row[8]),
                    "top_video_views": number(row[9]),
                }
            )
        i += 10
    if not videos or not channels:
        sys.exit("/radar/youtube: tables not found; the page layout may have changed.")
    return {
        "snapshot_date": snapshot.isoformat(),
        "source_url": f"{SITE}/radar/youtube",
        "counts_note": "YouTube public counts as reported to Radar's weekly crawl.",
        "new_channels": channels,
        "top_videos": videos,
    }


# ---------------------------------------------------------------- output


def short(n: int | None) -> str:
    if n is None:
        return "—"
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def md(value: str) -> str:
    return value.replace("|", "\\|")


def niche_table(niches: list[dict]) -> str:
    rows = [
        "| Niche | Category | Style | Format | What the videos are |",
        "|---|---|---|---|---|",
    ]
    for n in niches:
        cat = n["category"] or "—"
        if n["category_url"]:
            cat = f"[{cat}]({n['category_url']})"
        rows.append(
            f"| [{n['niche']}]({n['page_url']}) | {cat} | {n['style']} "
            f"| {n['format']} | {md(n['description'])} |"
        )
    return "\n".join(rows)


def channel_table(radar: dict) -> str:
    rows = [
        "| Channel | Style | Format | Channel age | Subscribers | Top video views |",
        "|---|---|---|---|---|---|",
    ]
    for c in radar["new_channels"][:NEW_CHANNEL_ROWS]:
        rows.append(
            f"| {md(c['channel'])} | {c['style']} | {c['format']} "
            f"| {c['channel_age_days']} days | {short(c['subscribers'])} "
            f"| {short(c['top_video_views'])} |"
        )
    return "\n".join(rows)


def ratio_table(radar: dict) -> str:
    top = sorted(
        (v for v in radar["top_videos"] if v["views_per_sub"]),
        key=lambda v: v["views_per_sub"],
        reverse=True,
    )[:VIEWS_PER_SUB_ROWS]
    rows = [
        "| Video | Channel | Style | Views | Views per subscriber |",
        "|---|---|---|---|---|",
    ]
    for v in top:
        rows.append(
            f"| {md(v['title'])} | {md(v['channel'])} | {v['style']} "
            f"| {short(v['views'])} | {v['views_per_sub']:g}× |"
        )
    return "\n".join(rows)


def replace_block(readme: str, name: str, body: str) -> str:
    pattern = re.compile(
        rf"(<!-- BEGIN {name} -->\n).*?(<!-- END {name} -->)", flags=re.S
    )
    if not pattern.search(readme):
        sys.exit(f"README.md: marker {name} missing.")
    return pattern.sub(lambda m: m.group(1) + body + "\n" + m.group(2), readme)


def write(niches: list[dict], radar: dict) -> None:
    today = dt.date.today().isoformat()
    meta = {
        "generated": today,
        "source": SITE,
        "license": "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)",
    }
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    (data / "niches.json").write_text(
        json.dumps({**meta, "niches": niches}, indent=2, ensure_ascii=False) + "\n"
    )
    (data / "radar.json").write_text(
        json.dumps({**meta, **radar}, indent=2, ensure_ascii=False) + "\n"
    )
    with (data / "niches.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(niches[0].keys()))
        writer.writeheader()
        writer.writerows(niches)

    readme_path = ROOT / "README.md"
    readme = readme_path.read_text()
    readme = replace_block(readme, "UPDATED", f"Last updated: {today}. Radar snapshot: {radar['snapshot_date']}.")
    readme = replace_block(readme, "NICHES", niche_table(niches))
    readme = replace_block(readme, "CHANNELS", channel_table(radar))
    readme = replace_block(readme, "RATIO", ratio_table(radar))
    readme_path.write_text(readme)


def main() -> None:
    niches = read_niches(published_niche_urls())
    radar = read_radar()
    for n in niches:
        n["examples_as_of"] = radar["snapshot_date"]
    if "--check" in sys.argv:
        print(json.dumps({"niches": niches, "radar": radar}, indent=2, ensure_ascii=False))
        return
    write(niches, radar)
    print(f"{len(niches)} niches, {len(radar['new_channels'])} new channels, "
          f"{len(radar['top_videos'])} top videos, snapshot {radar['snapshot_date']}")


if __name__ == "__main__":
    main()
