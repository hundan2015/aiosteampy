#!/usr/bin/env python3
"""Monitor node-steamcommunity and delegate applicable drift fixes to Copilot."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

UPSTREAM_OWNER = "DoctorMcKay"
UPSTREAM_REPOSITORY = "node-steamcommunity"
UPSTREAM = f"{UPSTREAM_OWNER}/{UPSTREAM_REPOSITORY}"
UPSTREAM_BRANCH = "master"
GITHUB_API = "https://api.github.com"
MODELS_API = "https://models.github.ai/inference/chat/completions"
COPILOT_LOGIN = "copilot-swe-agent"
QUALIFYING_CLASSIFICATIONS = {"STEAM_BEHAVIOR", "PORTABLE_BUG"}
VALID_CLASSIFICATIONS = QUALIFYING_CLASSIFICATIONS | {"UPSTREAM_ONLY", "MAINTENANCE", "UNCERTAIN"}
MAINTENANCE_PATHS = (
    "README",
    "docs/",
    ".github/",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "CHANGELOG",
    "LICENSE",
)


class DriftError(RuntimeError):
    """A recoverable monitor failure; callers must not advance state."""


class UpstreamHistoryRewrite(DriftError):
    """The stored cursor is not an ancestor of upstream HEAD."""


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    message: str
    parents: tuple[str, ...]
    files: tuple[str, ...]
    patch: str
    url: str


@dataclass(frozen=True)
class Classification:
    sha: str
    classification: str
    functional_area: str
    meaningful: bool
    confidence: float
    root_cause: str
    evidence: tuple[str, ...]
    local_targets: tuple[str, ...]
    suggested_tests: tuple[str, ...]

    @property
    def qualifies(self) -> bool:
        return (
            self.classification in QUALIFYING_CLASSIFICATIONS
            and self.meaningful
            and self.confidence >= 0.80
            and bool(self.evidence)
            and bool(self.local_targets)
        )


@dataclass(frozen=True)
class FunctionalGroup:
    functional_area: str
    classifications: tuple[Classification, ...]

    @property
    def newest_sha(self) -> str:
        return self.classifications[-1].sha


def initial_state(upstream: str = UPSTREAM, branch: str = UPSTREAM_BRANCH) -> dict[str, Any]:
    return {"upstream": upstream, "branch": branch, "last_processed_sha": None, "dispatched": {}}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return initial_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    required = {"upstream", "branch", "last_processed_sha", "dispatched"}
    if not required <= state.keys() or not isinstance(state["dispatched"], dict):
        raise DriftError(f"Invalid state file: {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dispatch_marker(functional_area: str, newest_sha: str) -> str:
    return f"upstream-drift:{functional_area}:{newest_sha}"


def is_maintenance_only(commit: Commit) -> bool:
    if not commit.files:
        return True
    return all(any(path == prefix or path.startswith(prefix) for prefix in MAINTENANCE_PATHS) for path in commit.files)


def group_meaningful(
    classifications: Iterable[Classification], order: dict[str, int]
) -> list[FunctionalGroup]:
    grouped: dict[str, list[Classification]] = defaultdict(list)
    for classification in classifications:
        if classification.qualifies:
            grouped[classification.functional_area].append(classification)
    return [
        FunctionalGroup(area, tuple(sorted(records, key=lambda record: order[record.sha])))
        for area, records in sorted(grouped.items())
    ]


class GitHubClient:
    def __init__(self, token: str | None, transport: Callable[..., Any] | None = None):
        self.token = token
        self.transport = transport or self._urlopen

    @staticmethod
    def _urlopen(request: Request) -> bytes:
        with urlopen(request, timeout=30) as response:  # nosec B310: fixed GitHub endpoints only
            return response.read()

    def request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str | None = None,
        accept: str = "application/vnd.github+json",
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any] | str:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Accept": accept,
            "User-Agent": "aiosteampy-upstream-drift",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if extra_headers:
            headers.update(extra_headers)
        credential = token if token is not None else self.token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        if body:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self.transport(request)
        except HTTPError as error:
            raise DriftError(f"GitHub API {method} {url} failed with HTTP {error.code}") from error
        except URLError as error:
            raise DriftError(f"GitHub API {method} {url} failed: {error.reason}") from error
        text = response.decode("utf-8") if isinstance(response, bytes) else response
        if accept == "application/vnd.github.patch":
            return text
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise DriftError(f"GitHub API {method} {url} returned invalid JSON") from error

    def upstream_head(self) -> str:
        data = self.request("GET", f"{GITHUB_API}/repos/{UPSTREAM}/git/ref/heads/{UPSTREAM_BRANCH}")
        return str(data["object"]["sha"])

    def compare(self, base: str, head: str, page: int) -> dict[str, Any]:
        return self.request(
            "GET",
            f"{GITHUB_API}/repos/{UPSTREAM}/compare/{quote(base)}...{quote(head)}?per_page=100&page={page}",
        )

    def commit(self, sha: str) -> Commit:
        data = self.request("GET", f"{GITHUB_API}/repos/{UPSTREAM}/commits/{sha}")
        detail = data["commit"]
        files = tuple(file["filename"] for file in data.get("files", []))
        patch = str(self.request("GET", f"https://github.com/{UPSTREAM}/commit/{sha}.patch", accept="application/vnd.github.patch"))
        return Commit(
            sha=data["sha"],
            date=detail["author"]["date"],
            message=detail["message"].splitlines()[0],
            parents=tuple(parent["sha"] for parent in data.get("parents", [])),
            files=files,
            patch=patch,
            url=data["html_url"],
        )


def resolve_range(client: GitHubClient, state: dict[str, Any], from_sha: str | None) -> tuple[str, list[Commit], bool]:
    head = client.upstream_head()
    base = from_sha or state["last_processed_sha"]
    if not base:
        return head, [], True
    commits: list[Commit] = []
    page = 1
    while True:
        comparison = client.compare(base, head, page)
        if comparison["status"] in {"behind", "diverged"}:
            raise UpstreamHistoryRewrite(f"Stored cursor {base} is not an ancestor of upstream HEAD {head}")
        records = comparison.get("commits", [])
        commits.extend(client.commit(record["sha"]) for record in records)
        if len(records) < 100:
            return head, commits, False
        page += 1


def parse_classification(sha: str, payload: dict[str, Any]) -> Classification:
    expected = {
        "classification",
        "functional_area",
        "meaningful",
        "confidence",
        "root_cause",
        "evidence",
        "local_targets",
        "suggested_tests",
    }
    if set(payload) != expected:
        raise DriftError("Model response does not match the classification schema")
    classification = payload["classification"]
    confidence = payload["confidence"]
    list_fields = ("evidence", "local_targets", "suggested_tests")
    if (
        classification not in VALID_CLASSIFICATIONS
        or not isinstance(payload["functional_area"], str)
        or not payload["functional_area"].strip()
        or not isinstance(payload["meaningful"], bool)
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not isinstance(payload["root_cause"], str)
        or not payload["root_cause"].strip()
        or any(
            not isinstance(payload[field], list)
            or not payload[field]
            or not all(isinstance(value, str) and value for value in payload[field])
            for field in list_fields
        )
    ):
        raise DriftError("Model response contains invalid classification values")
    return Classification(
        sha=sha,
        classification=classification,
        functional_area=payload["functional_area"].strip(),
        meaningful=payload["meaningful"],
        confidence=float(confidence),
        root_cause=payload["root_cause"].strip(),
        evidence=tuple(payload["evidence"]),
        local_targets=tuple(payload["local_targets"]),
        suggested_tests=tuple(payload["suggested_tests"]),
    )


def render_issue_body(group: FunctionalGroup, commits: dict[str, Commit]) -> str:
    marker = dispatch_marker(group.functional_area, group.newest_sha)
    annotations = "\n".join(f"Upstream: {UPSTREAM}@{record.sha}" for record in group.classifications)
    sources = "\n".join(
        f"- [{commits[record.sha].sha}]({commits[record.sha].url}): {commits[record.sha].message}"
        for record in group.classifications
    )
    evidence = "\n".join(
        f"- `{record.sha[:12]}`: {quote}" for record in group.classifications for quote in record.evidence
    )
    targets = "\n".join(f"- `{target}`" for record in group.classifications for target in record.local_targets)
    tests = "\n".join(f"- {test}" for record in group.classifications for test in record.suggested_tests)
    return f"""<!-- {marker} -->
## Upstream sources
{sources}

## Evidence
{evidence}

## Likely local targets
{targets}

## Required tests
{tests}

## Required commit provenance
Copy this block verbatim into the body of every substantive implementation commit. Do not replace it with PR-only attribution. Test-only or formatting-only commits do not repeat it.

```text
{annotations}
```

Verify aiosteampy is affected before editing. Fix the source cause, add behavioral tests, avoid unrelated refactors and dependencies, open one PR, and never merge it.
"""


def classify_commit(client: GitHubClient, prompt: str, commit: Commit) -> Classification:
    request = {
        "model": os.environ.get("UPSTREAM_CLASSIFIER_MODEL", "openai/gpt-4o-mini"),
        "temperature": 0,
        "messages": [{"role": "user", "content": f"{prompt}\n\nSHA: {commit.sha}\nURL: {commit.url}\nPatch:\n{commit.patch}"}],
    }
    for _ in range(2):
        try:
            response = client.request("POST", MODELS_API, payload=request)
            content = response["choices"][0]["message"]["content"]
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if match:
                return parse_classification(commit.sha, json.loads(match.group()))
        except (DriftError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            pass
    raise DriftError(f"Could not classify upstream commit {commit.sha}")


def ensure_label(client: GitHubClient, owner: str, repository: str, name: str) -> None:
    try:
        client.request("GET", f"{GITHUB_API}/repos/{owner}/{repository}/labels/{quote(name, safe='')}")
    except DriftError:
        client.request(
            "POST",
            f"{GITHUB_API}/repos/{owner}/{repository}/labels",
            payload={"name": name, "color": "0E8A16"},
        )


def create_issue(
    client: GitHubClient, owner: str, repository: str, group: FunctionalGroup, commits: dict[str, Commit]
) -> dict[str, Any]:
    marker = dispatch_marker(group.functional_area, group.newest_sha)
    query = quote(f"repo:{owner}/{repository} in:body \"{marker}\"")
    found = client.request("GET", f"{GITHUB_API}/search/issues?q={query}&per_page=1")
    if found["total_count"]:
        return found["items"][0]
    for label in ("upstream-drift", "copilot", "needs-human-review"):
        ensure_label(client, owner, repository, label)
    return client.request(
        "POST",
        f"{GITHUB_API}/repos/{owner}/{repository}/issues",
        payload={
            "title": f"[upstream drift] {group.functional_area}: adapt upstream behavior",
            "body": render_issue_body(group, commits),
            "labels": ["upstream-drift", "copilot", "needs-human-review"],
        },
    )


def assign_to_copilot(client: GitHubClient, token: str, owner: str, repository: str, issue_number: int) -> None:
    headers = {"GraphQL-Features": "issues_copilot_assignment_api_support,coding_agent_model_selection"}
    lookup = "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){id suggestedActors(capabilities:[CAN_BE_ASSIGNED],first:100){nodes{login ... on Bot{id} ... on User{id}}} issue(number:$number){id assignees(first:20){nodes{login}}}}}"
    data = client.request(
        "POST",
        f"{GITHUB_API}/graphql",
        payload={"query": lookup, "variables": {"owner": owner, "repo": repository, "number": issue_number}},
        token=token,
        extra_headers=headers,
    )["data"]["repository"]
    if COPILOT_LOGIN in {assignee["login"] for assignee in data["issue"]["assignees"]["nodes"]}:
        return
    actor = next((node for node in data["suggestedActors"]["nodes"] if node["login"] == COPILOT_LOGIN), None)
    if not actor:
        raise DriftError("Copilot cloud agent is not assignable")
    mutation = """mutation($issue: ID!, $actor: ID!, $repo: ID!, $base: String!) {
      addAssigneesToAssignable(input: {
        assignableId: $issue,
        actorIds: [$actor],
        agentAssignment: {
          targetRepositoryId: $repo,
          baseRef: $base,
          customInstructions: "Follow the issue and preserve every required Upstream line in substantive commit bodies."
        }
      }) {
        assignable { ... on Issue { assignees(first: 20) { nodes { login } } } }
      }
    }"""
    result = client.request(
        "POST",
        f"{GITHUB_API}/graphql",
        payload={"query": mutation, "variables": {"issue": data["issue"]["id"], "actor": actor["id"], "repo": data["id"], "base": os.environ.get("GITHUB_REF_NAME", "main")}},
        token=token,
        extra_headers=headers,
    )
    assignees = result["data"]["addAssigneesToAssignable"]["assignable"]["assignees"]["nodes"]
    if COPILOT_LOGIN not in {assignee["login"] for assignee in assignees}:
        raise DriftError("Copilot assignment did not persist")

def run_monitor(client: GitHubClient, state_path: Path, prompt_path: Path, from_sha: str | None, dry_run: bool, owner: str, repository: str, assignment_token: str | None) -> tuple[str, dict[str, Any]]:
    state = load_state(state_path)
    head, commits, initialized = resolve_range(client, state, from_sha)
    if initialized:
        if not dry_run:
            state["last_processed_sha"] = head
            save_state(state_path, state)
        return render_summary([], [], True), state
    prompt = prompt_path.read_text(encoding="utf-8")
    records: list[tuple[Commit, Classification | None]] = []
    classifications: list[Classification] = []
    for commit in commits:
        classification = None if is_maintenance_only(commit) else classify_commit(client, prompt, commit)
        records.append((commit, classification))
        if classification:
            classifications.append(classification)
    commit_map = {commit.sha: commit for commit in commits}
    order = {commit.sha: index for index, commit in enumerate(commits)}
    issue_urls = []
    for group in group_meaningful(classifications, order):
        if dry_run:
            continue
        if not assignment_token:
            raise DriftError("COPILOT_ASSIGN_TOKEN is required for a qualified change")
        issue = create_issue(client, owner, repository, group, commit_map)
        assign_to_copilot(client, assignment_token, owner, repository, int(issue["number"]))
        state["dispatched"][dispatch_marker(group.functional_area, group.newest_sha)] = {"issue": issue["number"], "commits": [item.sha for item in group.classifications]}
        issue_urls.append(issue["html_url"])
    if not dry_run:
        state["last_processed_sha"] = head
        save_state(state_path, state)
    return render_summary(records, issue_urls, False), state


def render_summary(records: list[tuple[Commit, Classification | None]], issue_urls: list[str], initialized: bool) -> str:
    lines = ["## Upstream drift monitor", "", f"Initialized cursor: {'yes' if initialized else 'no'}", "", "| SHA | Decision | Area |", "| --- | --- | --- |"]
    for commit, classification in records:
        if classification is None:
            lines.append(f"| `{commit.sha[:12]}` | maintenance-only | — |")
        else:
            lines.append(f"| `{commit.sha[:12]}` | {classification.classification} ({classification.confidence:.2f}) | {classification.functional_area} |")
    if issue_urls:
        lines.extend(["", "### Copilot issues", *[f"- {url}" for url in issue_urls]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".github/upstream-drift-state.json"))
    parser.add_argument("--prompt", type=Path, default=Path(".github/prompts/upstream-classifier.prompt.yml"))
    parser.add_argument("--from-sha")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", type=Path)
    arguments = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise DriftError("GITHUB_TOKEN is required")
    summary, _ = run_monitor(
        GitHubClient(token),
        arguments.state,
        arguments.prompt,
        arguments.from_sha,
        arguments.dry_run,
        os.environ.get("GITHUB_REPOSITORY_OWNER", "hundan2015"),
        os.environ.get("GITHUB_REPOSITORY", "hundan2015/aiosteampy").split("/", 1)[-1],
        os.environ.get("COPILOT_ASSIGN_TOKEN"),
    )
    destination = arguments.summary or (Path(os.environ["GITHUB_STEP_SUMMARY"]) if "GITHUB_STEP_SUMMARY" in os.environ else None)
    if destination:
        destination.write_text(summary, encoding="utf-8")
    else:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriftError as error:
        print(f"upstream-drift: {error}", file=sys.stderr)
        raise SystemExit(1)
