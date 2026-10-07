"""Summarize a public GitHub profile: top repos, languages, and README excerpts."""
import re

import httpx

from jobfinder.config import get_settings

API = "https://api.github.com"
_USER_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]+)/?", re.IGNORECASE)


class GitHubError(Exception):
    pass


def parse_username(url: str) -> str:
    m = _USER_RE.match(url.strip())
    if not m:
        raise GitHubError("Not a GitHub profile URL")
    return m.group(1)


def summarize_repos(username: str, repos: list[dict], readmes: dict[str, str]) -> str:
    own = [r for r in repos if not r.get("fork")]
    own.sort(key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at") or ""), reverse=True)
    langs: dict[str, int] = {}
    for r in own:
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
    lines = [f"GitHub user: {username}", f"Original repos: {len(own)}"]
    if langs:
        top = sorted(langs.items(), key=lambda kv: -kv[1])[:8]
        lines.append("Languages (by repo count): " + ", ".join(f"{k} ({v})" for k, v in top))
    lines.append("\nTop repositories:")
    for r in own[:15]:
        topics = ", ".join(r.get("topics") or [])
        lines.append(
            f"- {r['name']} [{r.get('language') or 'n/a'}, {r.get('stargazers_count', 0)} stars]"
            f": {r.get('description') or 'no description'}" + (f" (topics: {topics})" if topics else "")
        )
        if r["name"] in readmes:
            lines.append("  README: " + " ".join(readmes[r["name"]].split())[:600])
    return "\n".join(lines)


async def fetch_github_summary(url: str) -> str:
    username = parse_username(url)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "JobFinder"}
    if token := get_settings().github_token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        resp = await client.get(
            f"{API}/users/{username}/repos", params={"per_page": 100, "sort": "pushed"}
        )
        if resp.status_code == 404:
            raise GitHubError("GitHub user not found")
        if resp.status_code in (403, 429):
            raise GitHubError("GitHub rate limit hit; try again later")
        resp.raise_for_status()
        repos = resp.json()
        top = sorted(
            (r for r in repos if not r.get("fork")),
            key=lambda r: r.get("stargazers_count", 0),
            reverse=True,
        )[:5]
        readmes: dict[str, str] = {}
        for r in top:
            rr = await client.get(
                f"{API}/repos/{username}/{r['name']}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            if rr.status_code == 200:
                readmes[r["name"]] = rr.text[:3000]
    return summarize_repos(username, repos, readmes)
