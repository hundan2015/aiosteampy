# aiosteampy instructions

- This is an asynchronous Python Steam library. Use Python 3.10+ and preserve public APIs unless the issue requires an API change.
- Install project dependencies with Poetry when available. Run focused tests with `pytest`; do not depend on live Steam credentials for new unit tests.
- Upstream-drift issues reference node-steamcommunity. First verify that aiosteampy implements the affected surface and can reproduce the failure. Close the issue without code when the difference is Node-specific or out of scope.
- Fix the source cause. Do not hide failures with broad exception handling, change unrelated behavior, update dependencies, or refactor unrelated code.
- Add a deterministic behavioral regression test for every real change.
- A substantive commit implementing an upstream-drift issue MUST include the issue's exact ordered provenance block in its commit body:

  ```text
  Upstream: DoctorMcKay/node-steamcommunity@<40-character-SHA>
  ```

  Preserve one line per source commit, oldest first. Issue or PR attribution does not replace commit-body attribution. Test-only, formatting-only, and review-only commits do not repeat the block.
- Do not edit `.github/workflows/` unless the issue explicitly requires it. Never merge the pull request.
