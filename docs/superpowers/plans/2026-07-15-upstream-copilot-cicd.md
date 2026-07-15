# Upstream Copilot CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily, idempotent GitHub-native monitor that classifies node-steamcommunity changes and assigns qualified functional-area issues to Copilot cloud agent.

**Architecture:** A standard-library Python command owns deterministic state transitions and GitHub API boundaries; GitHub Actions only supplies schedule, permissions, secrets, and state commit. Pure functions cover filtering, model result validation, grouping, markers, and cursor decisions so the safety-critical behavior can be tested offline.

**Tech Stack:** Python 3.10+, pytest, urllib.request, GitHub REST/GraphQL APIs, GitHub Models chat completions, GitHub Actions.

---

## File Map

- Create `.github/scripts/upstream_drift.py`: domain records, GitHub HTTP client, commit harvesting, maintenance filtering, GitHub Models classification, grouping, issue creation, Copilot assignment, state update, CLI.
- Create `.github/prompts/upstream-classifier.prompt.yml`: versioned prompt text and JSON contract, loaded as plain text by the Python command.
- Create `.github/upstream-drift-state.json`: persisted upstream cursor and dispatch ledger.
- Create `.github/workflows/upstream-drift.yml`: daily/manual orchestration and state commit.
- Create `.github/copilot-instructions.md`: repository-specific build, scope, and safety instructions for cloud agent.
- Create `tests/test_upstream_drift.py`: offline behavior tests with mocked API boundaries.

### Task 1: Domain model, state, filtering, and grouping

**Files:**
- Create: `.github/scripts/upstream_drift.py`
- Create: `.github/upstream-drift-state.json`
- Test: `tests/test_upstream_drift.py`

- [ ] **Step 1: Write failing tests**

Add tests loading the script with `importlib.util.spec_from_file_location` and assert:

```python
def test_initial_state_starts_at_current_head_without_backfill(): ...
def test_mixed_source_commit_is_not_filtered(): ...
def test_docs_only_commit_is_filtered(): ...
def test_related_meaningful_commits_group_by_functional_area(): ...
def test_dispatch_marker_uses_area_and_newest_full_sha(): ...
```

Use concrete `Commit` and `Classification` fixtures. Assert oldest-first group order and exact marker `upstream-drift:econ-item-dates:<sha>`.

- [ ] **Step 2: Verify tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL because `.github/scripts/upstream_drift.py` does not exist.

- [ ] **Step 3: Implement minimal pure domain layer**

Define frozen dataclasses `Commit`, `Classification`, `FunctionalGroup`, plus `load_state`, `save_state`, `is_maintenance_only`, `group_meaningful`, and `dispatch_marker`. Maintenance filtering must require every changed path to be maintenance-class and must never discard mixed source changes.

Initialize state as:

```json
{
  "upstream": "DoctorMcKay/node-steamcommunity",
  "branch": "master",
  "last_processed_sha": null,
  "dispatched": {}
}
```

- [ ] **Step 4: Verify tests pass**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/scripts/upstream_drift.py .github/upstream-drift-state.json tests/test_upstream_drift.py
git commit -m "feat: add upstream drift domain model"
```

### Task 2: GitHub API harvesting and cursor safety

**Files:**
- Modify: `.github/scripts/upstream_drift.py`
- Test: `tests/test_upstream_drift.py`

- [ ] **Step 1: Write failing tests**

Add a fake transport and tests asserting:

```python
def test_collect_commits_returns_comparison_oldest_first(): ...
def test_collect_commits_rejects_non_ancestor_cursor(): ...
def test_first_run_initializes_head_without_model_calls(): ...
def test_from_sha_enables_backfill(): ...
```

Use GitHub compare fixtures with `status: "ahead"` and `commits` newest/oldest ordering. A `diverged` status must raise `UpstreamHistoryRewrite`.

- [ ] **Step 2: Verify tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL for missing `GitHubClient`, `collect_commits`, and `resolve_range`.

- [ ] **Step 3: Implement API and range resolution**

Implement a standard-library JSON client with explicit `User-Agent`, timeouts, response-size limits, and redacted exceptions. Add REST calls for branch HEAD, compare, commit details, and patch retrieval. Convert API data to `Commit`; refuse `diverged`/`behind`; initialize an empty cursor to HEAD unless `--from-sha` is supplied.

- [ ] **Step 4: Verify tests pass**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/scripts/upstream_drift.py tests/test_upstream_drift.py
git commit -m "feat: harvest upstream commit ranges safely"
```

### Task 3: GitHub Models classification contract

**Files:**
- Create: `.github/prompts/upstream-classifier.prompt.yml`
- Modify: `.github/scripts/upstream_drift.py`
- Test: `tests/test_upstream_drift.py`

- [ ] **Step 1: Write failing tests**

Add tests for `parse_classification` and the classifier boundary:

```python
def test_valid_steam_behavior_qualifies(): ...
def test_portable_bug_qualifies(): ...
def test_low_confidence_does_not_qualify(): ...
def test_missing_evidence_is_invalid(): ...
def test_unknown_category_is_invalid(): ...
def test_classifier_retries_one_invalid_response(): ...
def test_oversized_patch_is_split_by_file_hunks_not_truncated(): ...
```

A qualified result requires category in `{STEAM_BEHAVIOR, PORTABLE_BUG}`, `meaningful is True`, confidence `>= 0.80`, evidence, and local targets.

- [ ] **Step 2: Verify tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL for missing classification functions.

- [ ] **Step 3: Implement prompt and classifier**

Store strict category definitions and JSON-only schema in the prompt file. Implement GitHub Models POST to `https://models.github.ai/inference/chat/completions` with `GITHUB_TOKEN`, automatic model selection through configurable `UPSTREAM_CLASSIFIER_MODEL` defaulting to a generally available GitHub Models identifier, temperature 0, JSON extraction, schema validation, one retry, and patch chunk consolidation.

- [ ] **Step 4: Verify tests pass**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/prompts/upstream-classifier.prompt.yml .github/scripts/upstream_drift.py tests/test_upstream_drift.py
git commit -m "feat: classify upstream patches with GitHub Models"
```

### Task 4: Idempotent issue creation and Copilot assignment

**Files:**
- Modify: `.github/scripts/upstream_drift.py`
- Test: `tests/test_upstream_drift.py`

- [ ] **Step 1: Write failing tests**

Add fake REST/GraphQL responses and tests:

```python
def test_existing_issue_marker_is_reused(): ...
def test_issue_body_contains_all_commits_and_final_state(): ...
def test_missing_labels_are_created_once(): ...
def test_assignment_uses_auto_model_selection(): ...
def test_assignment_verifies_copilot_assignee(): ...
def test_assignment_failure_preserves_cursor(): ...
```

Assert `agentAssignment.model` is omitted or empty, target repository/base branch are explicit, and `copilot-swe-agent` is found through `suggestedActors`.
Add a provenance test asserting `render_issue_body` includes every source commit's full SHA, URL, and an oldest-first block of exact lines in this form:

```text
Upstream: DoctorMcKay/node-steamcommunity@<full 40-character SHA>
```

The rendered constraints must require Copilot to copy that block verbatim into every substantive local implementation commit, while excluding test-only and formatting-only commits.


- [ ] **Step 2: Verify tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL for missing issue and assignment operations.

- [ ] **Step 3: Implement dispatch**

Implement label reconciliation, marker search across open and closed issues, issue body rendering, GraphQL ID lookup, Copilot actor lookup, `replaceActorsForAssignable` with `agentAssignment`, and post-mutation assignee verification. Use `COPILOT_ASSIGN_TOKEN` only for GraphQL assignment. Reuse an existing unassigned issue and retry assignment.
Issue rendering must preserve full 40-character upstream SHAs and include the ordered `Upstream:` block. `.github/copilot-instructions.md` must establish that this block belongs in the local Git commit body, not merely the PR description, matching local commit `d713a9b`.

- [ ] **Step 4: Verify tests pass**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/scripts/upstream_drift.py tests/test_upstream_drift.py
git commit -m "feat: dispatch upstream fixes to Copilot"
```

### Task 5: CLI transaction, dry-run, and workflow summary

**Files:**
- Modify: `.github/scripts/upstream_drift.py`
- Test: `tests/test_upstream_drift.py`

- [ ] **Step 1: Write failing transaction tests**

Add tests:

```python
def test_dry_run_does_not_create_issue_or_write_state(): ...
def test_success_updates_cursor_after_all_assignments(): ...
def test_partial_dispatch_failure_does_not_write_state(): ...
def test_uncertain_classification_allows_cursor_advance(): ...
def test_summary_contains_commit_classification_and_issue_links(): ...
```

Inject clients and a state writer so tests observe exact call order and rollback behavior.

- [ ] **Step 2: Verify tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL for missing orchestration.

- [ ] **Step 3: Implement orchestration and CLI**

Add `run_monitor` and `main` with arguments `--state`, `--prompt`, `--from-sha`, `--dry-run`, and `--summary`. Classify every substantive commit, group qualified records, dispatch every group, then atomically write state only after all required assignments succeed. Write Markdown to `$GITHUB_STEP_SUMMARY` or the explicit summary path.

- [ ] **Step 4: Verify tests and CLI help**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.  
Run: `poetry run python .github/scripts/upstream_drift.py --help`  
Expected: exit 0 and list all five arguments.

- [ ] **Step 5: Commit**

```bash
git add .github/scripts/upstream_drift.py tests/test_upstream_drift.py
git commit -m "feat: orchestrate transactional upstream monitoring"
```

### Task 6: GitHub Actions workflow and Copilot instructions

**Files:**
- Create: `.github/workflows/upstream-drift.yml`
- Create: `.github/copilot-instructions.md`
- Modify: `tests/test_upstream_drift.py`

- [ ] **Step 1: Add workflow contract tests**

Read YAML as text without adding a YAML dependency. Assert it contains schedule `17 3 * * *`, `workflow_dispatch` inputs `from_sha` and `dry_run`, `models: read`, `issues: write`, `contents: write`, `cancel-in-progress: false`, secret `COPILOT_ASSIGN_TOKEN`, and a state commit guarded against dry-run.

- [ ] **Step 2: Verify workflow tests fail**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: FAIL because the workflow does not exist.

- [ ] **Step 3: Create workflow**

Checkout with full history, set up Python 3.10, run the monitor with environment tokens, and commit only `.github/upstream-drift-state.json` when changed and not dry-run. Use a stable concurrency group. Avoid third-party actions except official `actions/checkout` and `actions/setup-python`.

- [ ] **Step 4: Create Copilot repository instructions**

Document supported aiosteampy scope, module map, Poetry setup, relevant pytest command, source-cause requirement, behavioral-test requirement, no unrelated refactors, no dependency updates, and no workflow edits unless the issue explicitly requires them.
The instructions must require each substantive implementation commit to end with one exact `Upstream: DoctorMcKay/node-steamcommunity@<40-character SHA>` line per source commit, ordered oldest-first. PR/Issue attribution is supplementary only; test-only or formatting-only commits do not repeat the block.

- [ ] **Step 5: Verify tests pass**

Run: `poetry run pytest tests/test_upstream_drift.py -q`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/upstream-drift.yml .github/copilot-instructions.md tests/test_upstream_drift.py
git commit -m "ci: monitor upstream Steam behavior changes"
```

### Task 7: End-to-end offline verification

**Files:**
- Modify only if verification finds a defect.

- [ ] **Step 1: Run focused suite**

Run: `poetry run pytest tests/test_upstream_drift.py -v`  
Expected: all monitor tests pass.

- [ ] **Step 2: Run project unit tests**

Run: `poetry run pytest tests/test_utils.py tests/test_market.py -q`  
Expected: all selected existing tests pass without Steam credentials.

- [ ] **Step 3: Compile the command**

Run: `poetry run python -m py_compile .github/scripts/upstream_drift.py`  
Expected: exit 0.

- [ ] **Step 4: Exercise dry-run with fixture-safe boundary**

Run the CLI help and a mocked orchestration test rather than invoking live GitHub Models during local verification. Live dry-run requires repository `GITHUB_TOKEN` with `models: read` and is performed through `workflow_dispatch` after merge.

- [ ] **Step 5: Inspect workflow syntax in GitHub**

After pushing, run `workflow_dispatch` with `dry_run=true` and no `from_sha`. Expected: current upstream HEAD initializes or the existing cursor is checked; no issue, assignment, or state commit occurs; summary lists decisions.

- [ ] **Step 6: Controlled dispatch smoke test**

After adding `COPILOT_ASSIGN_TOKEN`, invoke a known backfill SHA range that contains one meaningful functional area. Expected: one labeled issue, Copilot assignee present, one Copilot PR, and no automatic merge.
