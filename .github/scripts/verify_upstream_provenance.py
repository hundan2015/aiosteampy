#!/usr/bin/env python3
"""Require source commit annotations on substantive upstream-drift PR commits."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.request import Request, urlopen

API = "https://api.github.com"
ANNOTATION = re.compile(r"^Upstream: DoctorMcKay/node-steamcommunity@[0-9a-f]{40}$", re.MULTILINE)
SUBSTANTIVE = re.compile(r"^(feat|fix|refactor|perf)(\([^)]*\))?:", re.IGNORECASE)


def request(url: str) -> dict:
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aiosteampy-upstream-drift",
    }
    with urlopen(Request(url, headers=headers), timeout=30) as response:  # nosec B310: GitHub API only
        return json.loads(response.read())


def main() -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    number = os.environ["PR_NUMBER"]
    pull = request(f"{API}/repos/{repository}/pulls/{number}")
    labels = {label["name"] for label in pull["labels"]}
    if "upstream-drift" not in labels:
        return 0
    commits = request(f"{API}/repos/{repository}/pulls/{number}/commits?per_page=100")
    missing = [
        commit["sha"]
        for commit in commits
        if SUBSTANTIVE.match(commit["commit"]["message"])
        and not ANNOTATION.search(commit["commit"]["message"])
    ]
    if missing:
        print("Substantive upstream-drift commits missing an Upstream annotation:", *missing, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
