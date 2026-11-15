#!/usr/bin/env python3
"""Rewrite the auto-generated parts of README.md from the GitHub API.

Three things are regenerated; everything else is left untouched:

  1. <!--START_SECTION:bench-->   … <!--END_SECTION:bench-->
     Table of the user's *pinned* repositories (GraphQL). Curate it by
     pinning/unpinning on github.com — never by editing the README.
     Falls back to the top non-fork repos by stars if nothing is pinned.

  2. <!--START_SECTION:shipped--> … <!--END_SECTION:shipped-->
     Latest public push per repo, newest first.

  3. The `languages:` line inside the ```yaml whoami block, computed from
     bytes of code across all owned, non-fork repos.

Plus a timestamp in <!--START_SECTION:updated--> … <!--END_SECTION:updated-->.

Standard library only. Needs GITHUB_TOKEN (the Actions default is enough)
and GH_USERNAME.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

USER = os.environ.get("GH_USERNAME", "ipwnds")
ORGS = [o.strip() for o in os.environ.get("GH_ORGS", "lumenworksco").split(",") if o.strip()]
EXCLUDE = {f"{USER}/{USER}".lower()}  # the profile repo itself (bot commits live there)
MAX_REPOS_SCANNED = 60
TOKEN = os.environ.get("GITHUB_TOKEN", "")
README = os.environ.get("README_PATH", "README.md")
API = "https://api.github.com"
MAX_SHIPPED = 5
MAX_BENCH = 6
MAX_LANGS = 5
NOT_LANGUAGES = {"Jupyter Notebook", "HTML", "CSS", "SCSS", "Shell", "Makefile", "Dockerfile", "Batchfile", "PowerShell"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": f"{USER}-profile-readme"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def get(url: str):
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def graphql(query: str, variables: dict):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{API}/graphql", data=body, method="POST",
        headers={**_headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def rel(ts: str) -> str:
    then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    d = datetime.now(timezone.utc) - then
    if d.days >= 365:
        return f"{d.days // 365}y ago"
    if d.days >= 30:
        return f"{d.days // 30}mo ago"
    if d.days >= 1:
        return f"{d.days}d ago"
    h = d.seconds // 3600
    return f"{h}h ago" if h else "just now"


def display(name: str) -> str:
    """Repo name for humans: strip leading sort-prefix punctuation like `-` or `-_`."""
    return name.lstrip("-_") or name


def cell(s: str | None, limit: int = 90) -> str:
    s = (s or "—").strip().replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def replace_section(text: str, name: str, body: str) -> str:
    pat = re.compile(rf"(<!--START_SECTION:{name}-->)(.*?)(<!--END_SECTION:{name}-->)", re.S)
    if not pat.search(text):
        print(f"marker '{name}' not found; skipping", file=sys.stderr)
        return text
    return pat.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)


def all_repos() -> list[dict]:
    """Public, non-fork, non-archived repos owned by the user or by ORGS."""
    sources = [f"{API}/users/{USER}/repos?per_page=100&type=owner&sort=pushed"]
    sources += [f"{API}/orgs/{o}/repos?per_page=100&type=public&sort=pushed" for o in ORGS]
    repos: dict[str, dict] = {}
    for url in sources:
        try:
            for r in get(url):
                if r.get("fork") or r.get("archived") or r.get("private"):
                    continue
                if r["full_name"].lower() in EXCLUDE:
                    continue
                repos[r["full_name"].lower()] = r
        except Exception as e:  # noqa: BLE001
            print(f"repos fetch failed for {url}: {e}", file=sys.stderr)
    out = sorted(repos.values(), key=lambda r: r.get("pushed_at", ""), reverse=True)
    return out[:MAX_REPOS_SCANNED]


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
PINNED_QUERY = """
query($login: String!) {
  user(login: $login) {
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes {
        ... on Repository {
          name url description isPrivate
          primaryLanguage { name }
          stargazerCount
        }
      }
    }
  }
}
"""


def bench_rows(repos: list[dict]) -> list[str]:
    nodes: list[dict] = []
    try:
        nodes = graphql(PINNED_QUERY, {"login": USER})["user"]["pinnedItems"]["nodes"]
        nodes = [n for n in nodes if n and not n.get("isPrivate")]
    except Exception as e:  # noqa: BLE001
        print(f"pinned fetch failed: {e}", file=sys.stderr)

    if not nodes:  # fallback: top repos by stars
        top = sorted(repos, key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at", "")), reverse=True)
        nodes = [{
            "name": r["name"], "url": r["html_url"], "description": r.get("description"),
            "primaryLanguage": {"name": r.get("language")} if r.get("language") else None,
            "stargazerCount": r.get("stargazers_count", 0),
        } for r in top[:MAX_BENCH]]

    rows = []
    for n in nodes[:MAX_BENCH]:
        lang = (n.get("primaryLanguage") or {}).get("name") or "—"
        stars = n.get("stargazerCount") or 0
        star_txt = f" ⭐ {stars}" if stars else ""
        rows.append(f"| [**{display(n['name'])}**]({n['url']}) | {cell(n.get('description'))} | {lang}{star_txt} |")
    return rows


def shipped_rows(repos: list[dict]) -> list[str]:
    """Latest commit on the default branch of each repo, newest first."""
    found: list[tuple[str, str]] = []
    for r in repos[:MAX_SHIPPED * 4]:  # already sorted by pushed_at; no need to scan everything
        try:
            commits = get(f"{API}/repos/{r['full_name']}/commits?per_page=1")
        except Exception as e:  # noqa: BLE001
            print(f"commits fetch failed for {r['full_name']}: {e}", file=sys.stderr)
            continue
        if not commits:
            continue
        c = commits[0]
        login = ((c.get("author") or {}).get("login") or "").lower()
        if login.endswith("[bot]"):
            continue
        date = c["commit"]["committer"]["date"]
        msg = c["commit"]["message"].splitlines()[0]
        row = f"| [{display(r['name'])}]({r['html_url']}) | [{cell(msg, 72)}]({c['html_url']}) | {rel(date)} |"
        found.append((date, row))
    found.sort(reverse=True)
    return [row for _, row in found[:MAX_SHIPPED]]


def language_line(repos: list[dict]) -> str | None:
    counter: Counter[str] = Counter()
    for r in repos:
        try:
            counter.update({k: v for k, v in get(r["languages_url"]).items() if k not in NOT_LANGUAGES})
        except Exception as e:  # noqa: BLE001
            print(f"languages fetch failed for {r['name']}: {e}", file=sys.stderr)
    if not counter:
        return None
    top = [name.lower() for name, _ in counter.most_common(MAX_LANGS)]
    return "languages:   [" + ", ".join(top) + "]"


def replace_yaml_key(text: str, key: str, line: str) -> str:
    """Replace the `key:` line inside the ```yaml block only."""
    block = re.compile(r"(```yaml\n)(.*?)(```)", re.S)
    m = block.search(text)
    if not m:
        print("yaml block not found; skipping languages", file=sys.stderr)
        return text
    inner = re.sub(rf"^{key}:.*$", line, m.group(2), count=1, flags=re.M)
    return text[: m.start(2)] + inner + text[m.end(2):]


# --------------------------------------------------------------------------- #
def main() -> int:
    repos = all_repos()

    bench = bench_rows(repos) or ["| — | pin a few repos on GitHub and this fills itself in | — |"]
    shipped = shipped_rows(repos) or ["| — | nothing public lately; the good stuff is still cooking | — |"]
    langs = language_line(repos)
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y")

    with open(README, encoding="utf-8") as f:
        original = f.read()

    updated = replace_section(original, "bench",
        "\n".join(["| Project | What it is | Lang |", "|:--|:--|:--|", *bench]))
    updated = replace_section(updated, "shipped",
        "\n".join(["| Repo | Last commit | When |", "|:--|:--|:--|", *shipped]))
    if langs:
        updated = replace_yaml_key(updated, "languages", langs)
    updated = replace_section(updated, "updated",
        f"<sub>pinned repos, languages and shipped list refresh nightly · last run {stamp}</sub>")

    if updated == original:
        print("README unchanged")
        return 0
    with open(README, "w", encoding="utf-8") as f:
        f.write(updated)
    print("README updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
