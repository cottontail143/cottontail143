#!/usr/bin/env python3
"""Render a terminal-style GitHub profile card from profile.json and live GitHub data."""

from __future__ import annotations
import calendar
import html
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "profile.json"
OUTPUT_PATHS = {"dark": ROOT / "dark_mode.svg", "light": ROOT / "light_mode.svg"}
WIDTH, HEIGHT = 840, 500
INFO_WIDTH = 56
PALETTES = {
    "dark": {
        "bg": "#0d1117", "border": "#30363d", "art": "#8b949e", "h": "#58a6ff",
        "k": "#ffa657", "v": "#c9d1d9", "d": "#484f58", "g": "#3fb950", "r": "#f85149",
    },
    "light": {
        "bg": "#ffffff", "border": "#d0d7de", "art": "#57606a", "h": "#0969da",
        "k": "#953800", "v": "#24292f", "d": "#afb8c1", "g": "#1a7f37", "r": "#cf222e",
    },
}

LOC_QUERY = """query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target { ...on Commit { history(first: 100, after: $cursor, author: {id: $id}) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
}}}}}}"""


# ---------------------------------------------------------------------------
# GitHub API & Statistics
# ---------------------------------------------------------------------------

def gh(url: str, payload: dict | None = None) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "{}")


def graphql(query: str, variables: dict | None = None) -> dict:
    res = gh("https://api.github.com/graphql", {"query": query, "variables": variables or {}})
    if res.get("errors"):
        raise RuntimeError(str(res["errors"]))
    return res["data"]


def line_changes(user: str, user_id: str, repos: list[str]) -> tuple[int, int]:
    adds = dels = 0
    for name in repos:
        cursor = None
        while True:
            branch = graphql(LOC_QUERY, {"owner": user, "name": name, "id": user_id, "cursor": cursor})["repository"]["defaultBranchRef"]
            if not branch:
                break
            hist = branch["target"]["history"]
            adds += sum(c["additions"] for c in hist["nodes"])
            dels += sum(c["deletions"] for c in hist["nodes"])
            if not hist["pageInfo"]["hasNextPage"]:
                break
            cursor = hist["pageInfo"]["endCursor"]
    return adds, dels


def fetch_stats(user: str) -> dict[str, int]:
    profile = gh(f"https://api.github.com/users/{user}")
    try:
        joined = datetime.fromisoformat(str(profile["created_at"]).replace("Z", "+00:00")).year
        contrib_fields = "\n".join(
            f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y + 1}-01-01T00:00:00Z")'
            " { totalCommitContributions restrictedContributionsCount }"
            for y in range(joined, datetime.now(timezone.utc).year + 1)
        )
        u = graphql(
            f"""query($login: String!) {{
              user(login: $login) {{
                id
                repositories(first: 100, ownerAffiliations: OWNER) {{
                  totalCount
                  nodes {{ name isFork }}
                }}
                {contrib_fields}
              }}
            }}""",
            {"login": user},
        )["user"]

        adds, dels = line_changes(user, u["id"], [r["name"] for r in u["repositories"]["nodes"] if not r["isFork"]])
        commits = sum(v["totalCommitContributions"] + v["restrictedContributionsCount"] for k, v in u.items() if k.startswith("y"))
        return {
            "repos": u["repositories"]["totalCount"],
            "commits": commits,
            "loc": adds - dels,
            "additions": adds,
            "deletions": dels,
        }
    except Exception as err:
        print(f"Could not refresh detailed GitHub stats: {err}")
        return {"repos": int(profile.get("public_repos", 0)), "commits": 0, "loc": 0, "additions": 0, "deletions": 0}


# Formatting & SVG Rendering
# ---------------------------------------------------------------------------
def uptime(birth_date: str) -> str:
    if not birth_date:
        return "Set birth_date in profile.json"
    b = date.fromisoformat(birth_date)
    t = date.today()
    y = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    m = (t.month - b.month - (t.day < b.day)) % 12
    if t.day >= b.day:
        d = t.day - b.day
    else:
        prev_month = t.month - 1 if t.month > 1 else 12
        prev_year = t.year if t.month > 1 else t.year - 1
        d = calendar.monthrange(prev_year, prev_month)[1] - b.day + t.day
    return f"{y} years, {m} months, {d} days"


def kv(key: str, val: object, width: int = INFO_WIDTH) -> list[tuple[str, str]]:
    dots = "." * max(width - len(key) - len(str(val)) - 3, 1)
    return [(f"{key}: ", "k"), (dots + " ", "d"), (str(val), "v")]


def rule(title: str = "") -> list[tuple[str, str]]:
    label = f"─ {title} " if title else ""
    return [(label, "h"), ("─" * (INFO_WIDTH - len(label)), "d")]


def loc_line(s: dict[str, int]) -> list[tuple[str, str]]:
    detail = f"{s['loc']:,} ({s['additions']:,}++, {s['deletions']:,}--)"
    dots = "." * max(INFO_WIDTH - len("Lines of Code") - len(detail) - 3, 1)
    return [
        ("Lines of Code: ", "k"), (dots + " ", "d"),
        (f"{s['loc']:,}", "v"), (" (", "d"),
        (f"{s['additions']:,}++", "g"), (", ", "d"),
        (f"{s['deletions']:,}--", "r"), (")", "d"),
    ]


def visible_lines(config: dict, stats: dict[str, int]) -> list[list[tuple[str, str]]]:
    p, c = config.get("profile", {}), config.get("contact", {})
    u = str(config["github_user"])
    lines = [
        [(f"{u}@github ", "h"), ("─" * (INFO_WIDTH - len(u) - 8), "d")],
        [],
    ]
    for k, v in p.items():
        if k == "_":
            lines.append([])
        elif k == "Uptime":
            lines.append(kv("Uptime", uptime(config.get("birth_date", ""))))
        elif v:
            lines.append(kv(k, v))
    lines += [
        [],
        rule("Contact"),
    ]
    lines += [kv(k, v) for k, v in c.items() if v]
    lines += [
        [],
        rule("GitHub Stats"),
        kv("Repos", stats["repos"]),
        kv("Commits", f"{stats['commits']:,}"),
        loc_line(stats),
    ]
    return lines

def render(mode: str, user: str, art: list[str], lines: list[list[tuple[str, str]]]) -> str:
    p = PALETTES[mode]
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        'font-family="Consolas, Menlo, monospace" font-size="13px" role="img" aria-labelledby="title description">',
        f'<title id="title">{html.escape(user)} GitHub profile</title>',
        '<desc id="description">A terminal-style profile with a configurable image rendered as ASCII art and live GitHub statistics.</desc>',
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="10" fill="{p["bg"]}" stroke="{p["border"]}"/>',
    ]
    for i, line in enumerate(art):
        svg.append(f'<text x="25" y="{40 + i * 15}" fill="{p["art"]}" xml:space="preserve">{html.escape(line)}</text>')
    for i, segs in enumerate(lines):
        if segs:
            spans = "".join(f'<tspan fill="{p[c]}">{html.escape(t)}</tspan>' for t, c in segs)
            svg.append(f'<text x="390" y="{45 + i * 21}" xml:space="preserve">{spans}</text>')
    svg.append("</svg>")
    return "\n".join(svg)


# ---------------------------------------------------------------------------
# Main Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    config = json.loads(CONFIG_PATH.read_text("utf-8"))
    user = str(config["github_user"])
    art_spec = config.get("profile_image", [])
    art = (ROOT / art_spec).read_text().splitlines() if isinstance(art_spec, str) else art_spec
    preview = "--preview" in sys.argv
    stats = {"repos": 0, "commits": 0, "loc": 0, "additions": 0,"deletions": 0} if preview else fetch_stats(user)
    if not preview:
        print("stats:", stats)
    lines = visible_lines(config, stats)
    for mode, out_path in OUTPUT_PATHS.items():
        out_path.write_text(render(mode, user, art, lines), encoding="utf-8")
        print(f"Rendered {out_path.name}")


if __name__ == "__main__":
    main()
