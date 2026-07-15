import sys
import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / ".github" / "scripts" / "upstream_drift.py"


def load_module():
    spec = importlib.util.spec_from_file_location("upstream_drift", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_initial_state_starts_at_current_head_without_backfill():
    drift = load_module()
    state = drift.initial_state("DoctorMcKay/node-steamcommunity", "master")

    assert state == {
        "upstream": "DoctorMcKay/node-steamcommunity",
        "branch": "master",
        "last_processed_sha": None,
        "dispatched": {},
    }


def test_mixed_source_commit_is_not_filtered():
    drift = load_module()
    commit = drift.Commit(
        sha="a" * 40,
        date="2026-07-15T00:00:00Z",
        message="Update parser and lockfile",
        parents=("b" * 40,),
        files=("classes/CEconItem.js", "package-lock.json"),
        patch="diff --git a/classes/CEconItem.js b/classes/CEconItem.js\n+parseNewDate()",
        url="https://example.test/commit",
    )

    assert drift.is_maintenance_only(commit) is False


def test_docs_only_commit_is_filtered():
    drift = load_module()
    commit = drift.Commit(
        sha="a" * 40,
        date="2026-07-15T00:00:00Z",
        message="Update documentation",
        parents=("b" * 40,),
        files=("README.md", "docs/api.md"),
        patch="diff --git a/README.md b/README.md\n+new docs",
        url="https://example.test/commit",
    )

    assert drift.is_maintenance_only(commit) is True


def test_related_meaningful_commits_group_by_functional_area():
    drift = load_module()
    older = drift.Classification(
        sha="1" * 40,
        classification="STEAM_BEHAVIOR",
        functional_area="econ-item-dates",
        meaningful=True,
        confidence=0.95,
        root_cause="Steam changed the date format.",
        evidence=("+day-first parser",),
        local_targets=("aiosteampy/models.py",),
        suggested_tests=("parse day-first date",),
    )
    newer = drift.Classification(
        sha="2" * 40,
        classification="PORTABLE_BUG",
        functional_area="econ-item-dates",
        meaningful=True,
        confidence=0.91,
        root_cause="The first parser did not retain the legacy format.",
        evidence=("+fallback parser",),
        local_targets=("aiosteampy/models.py",),
        suggested_tests=("parse both formats",),
    )

    groups = drift.group_meaningful([newer, older], {older.sha: 1, newer.sha: 2})

    assert len(groups) == 1
    assert groups[0].functional_area == "econ-item-dates"
    assert [record.sha for record in groups[0].classifications] == [older.sha, newer.sha]
    assert groups[0].newest_sha == newer.sha


def test_dispatch_marker_uses_area_and_newest_full_sha():
    drift = load_module()
    assert drift.dispatch_marker("econ-item-dates", "a" * 40) == f"upstream-drift:econ-item-dates:{'a' * 40}"


def test_valid_steam_behavior_qualifies():
    drift = load_module()
    result = drift.parse_classification(
        "a" * 40,
        {
            "classification": "STEAM_BEHAVIOR",
            "functional_area": "mobileconf-ua",
            "meaningful": True,
            "confidence": 0.95,
            "root_cause": "Steam rejects requests without a mobile user agent.",
            "evidence": ["+ 'user-agent': 'okhttp/4.9.2'"],
            "local_targets": ["aiosteampy/mixins/confirmation.py"],
            "suggested_tests": ["mobileconf request has user agent"],
        },
    )

    assert result.qualifies is True


def test_missing_evidence_is_invalid():
    drift = load_module()
    payload = {
        "classification": "STEAM_BEHAVIOR",
        "functional_area": "mobileconf-ua",
        "meaningful": True,
        "confidence": 0.95,
        "root_cause": "Steam changed behavior.",
        "evidence": [],
        "local_targets": ["aiosteampy/mixins/confirmation.py"],
        "suggested_tests": ["request has user agent"],
    }

    import pytest

    with pytest.raises(drift.DriftError):
        drift.parse_classification("a" * 40, payload)


def test_issue_body_preserves_full_upstream_commit_provenance():
    drift = load_module()
    sha = "a" * 40
    commit = drift.Commit(
        sha=sha,
        date="2026-07-15T00:00:00Z",
        message="Use mobile user agent",
        parents=(),
        files=("components/confirmations.js",),
        patch="+ user-agent",
        url=f"https://github.com/{drift.UPSTREAM}/commit/{sha}",
    )
    classification = drift.Classification(
        sha=sha,
        classification="STEAM_BEHAVIOR",
        functional_area="mobileconf-ua",
        meaningful=True,
        confidence=0.95,
        root_cause="Steam now rejects a missing mobile user agent.",
        evidence=("+ user-agent",),
        local_targets=("aiosteampy/mixins/confirmation.py",),
        suggested_tests=("mobileconf requests include the user agent",),
    )

    body = drift.render_issue_body(drift.FunctionalGroup("mobileconf-ua", (classification,)), {sha: commit})

    assert sha in body
    assert commit.url in body
    assert f"Upstream: {drift.UPSTREAM}@{sha}" in body


def test_workflow_requires_dry_run_gate_and_minimal_permissions():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "upstream-drift.yml").read_text(encoding="utf-8")

    assert "cron: '17 3 * * *'" in workflow
    assert "models: read" in workflow
    assert "issues: write" in workflow
    assert "COPILOT_ASSIGN_TOKEN" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "if: ${{ !inputs.dry_run }}" in workflow


def test_dry_run_never_writes_initialized_cursor(tmp_path):
    drift = load_module()

    class HeadClient:
        def upstream_head(self):
            return "a" * 40

    state_path = tmp_path / "state.json"
    drift.save_state(state_path, drift.initial_state())
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("classify", encoding="utf-8")

    summary, state = drift.run_monitor(
        HeadClient(), state_path, prompt_path, None, True, "hundan2015", "aiosteampy", None
    )

    assert state["last_processed_sha"] is None
    assert drift.load_state(state_path)["last_processed_sha"] is None
    assert "Initialized cursor: yes" in summary
