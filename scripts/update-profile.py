#!/usr/bin/env python3
"""Build profile cards from GitHub's public data using the standard library.

The two cards intentionally measure different things. Contributions use GitHub's
contribution rules; time of day uses authored, non-merge commits on the default
branches of publicly owned, non-fork repositories. Commit time is an activity
proxy, not a measurement of time spent coding. No private contribution count is
included. A failed collection leaves the last successful assets untouched.

Normal use: GITHUB_TOKEN=... python3 scripts/update-profile.py --user sjs9668
Public REST preview: python3 scripts/update-profile.py --user sjs9668 --rest-only
Render previously collected data: python3 scripts/update-profile.py --render-json FILE
"""

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


UTC = timezone.utc
KST = timezone(timedelta(hours=9), name="Asia/Seoul")
BUCKETS = (
    ("Morning", "06:00-12:00", 6, 12),
    ("Daytime", "12:00-18:00", 12, 18),
    ("Evening", "18:00-24:00", 18, 24),
    ("Night", "00:00-06:00", 0, 6),
)
QUERY = """
query ProfileStats($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      startedAt endedAt
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      totalRepositoriesWithContributedCommits
    }
  }
}
"""


class CollectionError(RuntimeError):
    pass


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CollectionError("GitHub returned a timestamp without a timezone.")
    return parsed.astimezone(UTC)


class GitHub:
    def __init__(self, token):
        self.token = token
        self.request_count = 0

    def request(self, endpoint, payload=None, empty_repository=False, method=None):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "sjs9668-profile-stats",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        url = "https://api.github.com" + endpoint
        for attempt in range(3):
            self.request_count += 1
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                    return json.load(response)
            except HTTPError as error:
                # GitHub returns 409 for repositories with no commit history.
                if empty_repository and error.code == 409:
                    return []
                if (error.code == 429 or error.code >= 500) and attempt < 2:
                    time.sleep(min(10, 2 ** (attempt + 1)))
                    continue
                raise CollectionError(
                    f"GitHub API returned HTTP {error.code} for {endpoint.split('?')[0]}. "
                    "Assets were not updated. Check permissions and API rate limits."
                ) from None
            except (URLError, TimeoutError) as error:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise CollectionError("GitHub API could not be reached; assets were not updated.") from error
        raise CollectionError("GitHub API retry limit reached.")

    def pages(self, endpoint, params, empty_repository=False):
        page = 1
        while True:
            values = self.request(
                endpoint + "?" + urlencode(dict(params, per_page=100, page=page)),
                empty_repository=empty_repository,
            )
            if not isinstance(values, list):
                raise CollectionError("Unexpected GitHub REST response; assets were not updated.")
            yield from values
            if len(values) < 100:
                return
            page += 1


def contribution_stats(api, username, start, end):
    result = api.request("/graphql", {
        "query": QUERY,
        "variables": {"login": username, "from": timestamp(start), "to": timestamp(end)},
    })
    if result.get("errors"):
        # Do not print server responses, which may contain request information.
        raise CollectionError("GitHub GraphQL query failed; assets were not updated.")
    user = result.get("data", {}).get("user")
    if not user:
        raise CollectionError("GitHub user was not found; assets were not updated.")
    values = user["contributionsCollection"]
    fields = {
        "commits": "totalCommitContributions",
        "pull_requests": "totalPullRequestContributions",
        "issues": "totalIssueContributions",
        "repositories_with_commits": "totalRepositoriesWithContributedCommits",
    }
    stats = {key: values[field] for key, field in fields.items()}
    if any(type(value) is not int or value < 0 for value in stats.values()):
        raise CollectionError("GitHub returned invalid contribution counts.")
    stats["status"] = "available"
    stats["source"] = "GitHub GraphQL contributionsCollection (public contributions only)"
    return stats


def bucket_for_hour(hour):
    for name, _, first, last in BUCKETS:
        if first <= hour < last:
            return name
    raise ValueError("Hour must be in the range 0 through 23.")


def commit_activity(api, username, repositories, start, end):
    counts = {name: 0 for name, _, _, _ in BUCKETS}
    seen = set()
    repositories_with_samples = 0
    excluded_merges = 0
    for repository in repositories:
        samples_before = len(seen)
        full_name = repository["full_name"]
        params = {
            "author": username,
            "since": timestamp(start),
            "until": timestamp(end),
            "sha": repository["default_branch"],
        }
        for commit in api.pages(
            "/repos/" + quote(full_name, safe="/") + "/commits",
            params,
            empty_repository=True,
        ):
            author = commit.get("author") or {}
            if str(author.get("login", "")).casefold() != username.casefold():
                continue
            authored_at = (commit.get("commit", {}).get("author") or {}).get("date")
            if not authored_at:
                raise CollectionError("A commit author timestamp is missing; assets were not updated.")
            authored_at = parse_timestamp(authored_at)
            # REST's since/until filter is supplemented with the author date,
            # which is the date actually used to calculate the displayed hour.
            if not start <= authored_at <= end:
                continue
            if len(commit.get("parents", [])) > 1:
                excluded_merges += 1
                continue
            sha = commit["sha"]
            if sha in seen:
                continue
            seen.add(sha)
            counts[bucket_for_hour(authored_at.astimezone(KST).hour)] += 1
        if len(seen) > samples_before:
            repositories_with_samples += 1
    total = sum(counts.values())
    return {
        "status": "available" if total else "no_samples",
        "timezone": "Asia/Seoul",
        "utc_offset": "+09:00",
        "sample_size": total,
        "repositories_scanned": len(repositories),
        "repositories_with_samples": repositories_with_samples,
        "excluded_merge_records": excluded_merges,
        "scope": "Public owned non-fork repositories; default branches only",
        "timestamp": "Commit author timestamp, converted to Asia/Seoul",
        "exclusions": "Merge commits excluded; duplicate commit SHAs counted once",
        "interpretation": "Commit activity is a proxy, not hours spent coding",
        "buckets": [
            {
                "label": name,
                "hours": hours,
                "commits": counts[name],
                "percentage": round(counts[name] / total * 100, 1) if total else 0.0,
            }
            for name, hours, _, _ in BUCKETS
        ],
    }


def collect(api, username, now, rest_only=False):
    end = now.astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(days=365)
    stats = (
        {"status": "unavailable", "reason": "REST-only preview; GraphQL requires GITHUB_TOKEN"}
        if rest_only else contribution_stats(api, username, start, end)
    )
    repositories = [
        repository for repository in api.pages(
            "/users/" + quote(username, safe="") + "/repos",
            {"type": "owner", "sort": "full_name"},
        )
        if not repository.get("fork") and not repository.get("private")
        and repository.get("owner", {}).get("login", "").casefold() == username.casefold()
    ]
    stats["stars"] = sum(repository.get("stargazers_count", 0) for repository in repositories)
    stats["stars_scope"] = "Current stars across public owned non-fork repositories"
    activity = commit_activity(api, username, repositories, start, end)
    data = {
        "schema_version": 1,
        "username": username,
        "generated_at": timestamp(end),
        "period": {"from": timestamp(start), "to": timestamp(end), "days": 365},
        "stats": stats,
        "commit_activity": activity,
        "api_requests": api.request_count,
    }
    data["coding_gist_title"] = coding_gist_title(data)
    data["stats_gist_title"] = username.upper() + "'s GitHub Stats"
    return data


def coding_gist_title(data):
    counts = {bucket["label"]: bucket["commits"] for bucket in data["commit_activity"]["buckets"]}
    if not data["commit_activity"]["sample_size"]:
        return "My commit activity 🕒"
    if counts["Morning"] + counts["Daytime"] > counts["Evening"] + counts["Night"]:
        return "I'm an early 🐤"
    return "I'm a night 🦉"


def svg_card(title, description, body, height=250):
    # SVG remains readable in both GitHub themes. There is no external font,
    # script, image, foreignObject or third-party runtime dependency.
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="495" height="{height}" viewBox="0 0 495 {height}" role="img" aria-labelledby="title desc">
  <title id="title">{escape(title)}</title>
  <desc id="desc">{escape(description)}</desc>
  <style>
    .bg {{ fill: #ffffff; stroke: #d0d7de; }}
    .text {{ fill: #24292f; }}
    .muted {{ fill: #57606a; }}
    .accent {{ fill: #0969da; }}
    .track {{ fill: #eaeef2; }}
    text {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }}
    .title {{ font-size: 15px; font-weight: 600; }}
    .small {{ font-size: 10px; }}
    @media (prefers-color-scheme: dark) {{
      .bg {{ fill: #0d1117; stroke: #30363d; }}
      .text {{ fill: #e6edf3; }}
      .muted {{ fill: #8b949e; }}
      .accent {{ fill: #58a6ff; }}
      .track {{ fill: #21262d; }}
    }}
  </style>
  <rect class="bg" x="0.5" y="0.5" width="494" height="{height - 1}" rx="6"/>
{body}
</svg>
'''


def text_node(x, y, value, css="text", anchor=None):
    align = f' text-anchor="{anchor}"' if anchor else ""
    return f'  <text x="{x}" y="{y}" class="{css}"{align}>{escape(str(value))}</text>'


def period_label(data):
    start = parse_timestamp(data["period"]["from"]).astimezone(KST).date()
    end = parse_timestamp(data["period"]["to"]).astimezone(KST).date()
    return f"{start} - {end}"


def stats_svg(data):
    stats = data["stats"]
    period = period_label(data)
    lines = [
        text_node(20, 28, f'{data["username"]}\'s GitHub Stats', "text title"),
        text_node(20, 48, "Public contributions / last 365 days", "muted small"),
    ]
    rows = (
        ("Stars earned (current)", stats.get("stars")),
        ("Commits", stats.get("commits")),
        ("Pull requests", stats.get("pull_requests")),
        ("Issues opened", stats.get("issues")),
        ("Repositories with commits", stats.get("repositories_with_commits")),
    )
    for index, (label, value) in enumerate(rows):
        y = 80 + index * 25
        lines.append(text_node(20, y, label))
        lines.append(text_node(473, y, f"{value:,}" if value is not None else "Unavailable", "accent", "end"))
    lines.extend([
        text_node(20, 214, period + " / Asia/Seoul", "muted small"),
        text_node(20, 233, "Stars: public owned non-fork repos / GitHub API", "muted small"),
    ])
    description = (
        f"{data['username']} public GitHub statistics. {period} Asia/Seoul. "
        + "; ".join(f"{label}: {value if value is not None else 'unavailable'}" for label, value in rows)
        + ". Contributions follow GitHub contribution counting rules; stars are current."
    )
    return svg_card("GitHub Stats", description, "\n".join(lines))


def coding_svg(data):
    activity = data["commit_activity"]
    total = activity["sample_size"]
    period = period_label(data)
    lines = [
        text_node(20, 28, "When I commit", "text title"),
        text_node(20, 48, f"Asia/Seoul (UTC+09:00) / {total:,} sampled commits", "muted small"),
    ]
    for index, bucket in enumerate(activity["buckets"]):
        y = 80 + index * 27
        width = 130 * bucket["commits"] / total if total else 0
        lines.extend([
            text_node(20, y, bucket["label"]),
            text_node(100, y, bucket["hours"], "muted small"),
            f'  <rect x="184" y="{y - 9}" width="130" height="8" rx="2" class="track"/>',
            f'  <rect x="184" y="{y - 9}" width="{width:.2f}" height="8" rx="2" class="accent"/>',
            text_node(396, y, f'{bucket["commits"]:,}', "text", "end"),
            text_node(473, y, f'{bucket["percentage"]:.1f}%', "muted", "end"),
        ])
    lines.extend([
        text_node(20, 194, period + f" / {activity['repositories_scanned']} repos", "muted small"),
        text_node(20, 211, "Public owned non-forks / default branches / author time", "muted small"),
        text_node(20, 228, "Merge commits excluded / duplicate SHAs counted once", "muted small"),
        text_node(20, 245, "Commit activity proxy; not time spent coding", "muted small"),
    ])
    if not total:
        lines.append(text_node(473, 28, "No samples", "muted small", "end"))
    description = (
        f"Commit activity from {period}, Asia/Seoul. Sample: {total} authored commits "
        f"in {activity['repositories_scanned']} public owned non-fork repositories, default branches only. "
        "Merge commits excluded; duplicate SHAs counted once. "
        + "; ".join(f"{item['label']} {item['hours']}: {item['commits']} ({item['percentage']} percent)" for item in activity["buckets"])
        + ". These are commit times, not measured coding hours."
    )
    return svg_card("When I commit (Asia/Seoul)", description, "\n".join(lines), height=263)


def stats_text(data):
    stats = data["stats"]
    rows = (
        ("⭐ Stars", stats.get("stars")),
        ("📝 Commits", stats.get("commits")),
        ("🔀 Pull requests", stats.get("pull_requests")),
        ("🐞 Issues", stats.get("issues")),
        ("📁 Repos with commits", stats.get("repositories_with_commits")),
    )
    lines = [f"{label:<23} {value:,}" if value is not None else f"{label:<23} Unavailable" for label, value in rows]
    lines.extend([
        "",
        f"Period: {period_label(data)} (last 365 days)",
        "Contributions: public GitHub contribution counts.",
        "Stars: current total of public owned non-fork repos.",
        "Repos: distinct repositories with commit contributions.",
        "Updated: " + parse_timestamp(data["generated_at"]).astimezone(KST).strftime("%Y-%m-%d %H:%M Asia/Seoul"),
    ])
    return "\n".join(lines) + "\n"


def coding_text(data):
    activity = data["commit_activity"]
    total = activity["sample_size"]
    icons = ("🌞", "🏙", "🌆", "🌙")
    lines = []
    for icon, bucket in zip(icons, activity["buckets"]):
        filled = round(20 * bucket["commits"] / total) if total else 0
        bar = "█" * filled + "░" * (20 - filled)
        lines.append(f"{icon} {bucket['label']:<8} {bucket['commits']:>5,} commits  {bar} {bucket['percentage']:>5.1f}%")
    lines.extend([
        "",
        f"Period: {period_label(data)} (last 365 days)",
        f"Timezone: Asia/Seoul (UTC+09:00); sample: {total:,} commits.",
        f"Scope: {activity['repositories_scanned']} public owned non-fork repos, default branches.",
        "Author timestamps; merge commits excluded; SHAs deduplicated.",
        "Morning 06-12 / Daytime 12-18 / Evening 18-24 / Night 00-06.",
        "Commit activity is a proxy, not measured coding hours.",
        "Updated: " + parse_timestamp(data["generated_at"]).astimezone(KST).strftime("%Y-%m-%d %H:%M Asia/Seoul"),
    ])
    if not total:
        lines.append("No matching commits were found in this period and scope.")
    return "\n".join(lines) + "\n"


def sync_gists(data):
    """Gists require a separate, explicit token; never use GITHUB_TOKEN here."""
    token = os.environ.get("GIST_TOKEN")
    if not token:
        print("Gist synchronization skipped: GIST_TOKEN is not configured.")
        return
    config_path = Path(__file__).resolve().parent / "gists.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    coding_id = os.environ.get("CODING_GIST_ID") or config.get("coding_gist_id")
    stats_id = os.environ.get("STATS_GIST_ID") or config.get("stats_gist_id")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{20,40}", value) for value in (coding_id, stats_id)):
        raise CollectionError("GIST_TOKEN is set but two valid Gist IDs are not configured.")
    api = GitHub(token)
    for gist_id, title, content in (
        (coding_id, data.get("coding_gist_title") or coding_gist_title(data), coding_text(data)),
        (stats_id, data.get("stats_gist_title") or data["username"].upper() + "'s GitHub Stats", stats_text(data)),
    ):
        # Pinned Gists display the filename, so rename the existing file rather
        # than adding a second file whose order would make the title ambiguous.
        gist = api.request("/gists/" + gist_id)
        files = gist.get("files", {})
        if len(files) != 1:
            raise CollectionError("Each statistics Gist must contain exactly one file.")
        previous_filename = next(iter(files))
        api.request(
            "/gists/" + gist_id,
            {"files": {previous_filename: {"filename": title, "content": content}}},
            method="PATCH",
        )
    print("Updated both configured Gists.")


def write_assets(data, directory):
    directory.mkdir(parents=True, exist_ok=True)
    contents = {
        "profile-stats.svg": stats_svg(data),
        "coding-time.svg": coding_svg(data),
        "profile-stats.txt": stats_text(data),
        "coding-time.txt": coding_text(data),
        "profile-data.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    }
    staged = []
    try:
        # Render all assets before replacing any successful previous output.
        for name, content in contents.items():
            path = directory / name
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8", newline="\n")
            staged.append((temporary, path))
        for temporary, path in staged:
            temporary.replace(path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "sjs9668")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "assets")
    parser.add_argument("--rest-only", action="store_true", help="Preview public REST data; contribution totals are explicitly unavailable")
    parser.add_argument("--render-json", type=Path, help="Render an existing collected JSON document without API calls")
    args = parser.parse_args()
    try:
        if args.render_json:
            data = json.loads(args.render_json.read_text(encoding="utf-8"))
        else:
            token = os.environ.get("GITHUB_TOKEN")
            if not token and not args.rest_only:
                raise CollectionError("Set GITHUB_TOKEN, or use --rest-only for a public REST preview.")
            data = collect(GitHub(token), args.user, datetime.now(UTC), rest_only=args.rest_only)
        write_assets(data, args.output_dir)
        if not args.rest_only and not args.render_json:
            sync_gists(data)
    except (CollectionError, OSError, ValueError, KeyError) as error:
        print(f"Profile update failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Updated profile assets for {data['username']}: "
        f"{data['commit_activity']['sample_size']} sampled commits, "
        f"{data.get('api_requests', 'unknown')} API requests."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
