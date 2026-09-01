# Releasing jmunch-mcp

This repo has **no CI**. There are no workflows, so nothing here enforces any of
the steps below. The local suite is the only gate that exists. Read this file
before every release.

The sibling repos (jcodemunch, jdocmunch, jdatamunch) are covered by the shared
`release` skill. This repo is not in that skill's scope but shares its build
stack, so it carries the same hazards with none of the automation.

## Steps

1. **Bump the version.** There are **two** pin sites: `pyproject.toml`, and
   `server.json` - which carries it **twice**, at the top level and inside
   `packages[]`. The registry accepts one moving without the other.

   `tests/test_version_sync.py` gates all three values and fails when a *new*
   file starts carrying the version, so the enumeration below is a cross-check
   rather than the thing you rely on:

   ```bash
   grep -rn "<old-version>" --include=*.json --include=*.toml --include=*.lock .
   ```

2. **Date the CHANGELOG.** Change `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`.

3. **Run the full suite.** `PYTHONPATH=src` is required. Without it the
   installed package shadows `src/` and you test the wrong code.

   ```bash
   PYTHONPATH=src python -m pytest tests/ -q
   ```

   Read the **skip count**, not only the exit code. A jump in skips means the
   environment changed, not the code.

4. **Run the linter.** `uv run ruff check src/ tests/`. It is not wired to
   anything, so it fails silently by never running. **Expect zero findings.**
   The repo was brought to clean on 2026-09-01, so any finding is new.

5. **Commit and push** before any irreversible step.

6. **Clean the working tree, then build.** See "The build reads your working
   tree" below. This step is not optional.

   ```bash
   git stash push -u <any dirty paths>
   rm -f dist/*
   uvx --from build pyproject-build
   ```

7. **Check, then upload.**

   ```bash
   uvx --from twine twine check  dist/*X.Y.Z*
   uvx --from twine twine upload dist/*X.Y.Z*
   ```

8. **Tag and release.** Verify the branch first; do not type it from memory.

   ```bash
   BR=$(GITHUB_TOKEN="" gh repo view jgravelle/jmunch-mcp --json defaultBranchRef -q .defaultBranchRef.name)
   git rev-parse --abbrev-ref HEAD    # must equal $BR
   git tag vX.Y.Z && GITHUB_TOKEN="" git push origin vX.Y.Z
   GITHUB_TOKEN="" gh release create vX.Y.Z dist/*X.Y.Z* --repo jgravelle/jmunch-mcp --title "..." --notes "..."
   ```

9. **Restore anything you stashed in step 6.**

## Use uvx, not `python -m twine`

⚠ `python -m twine` is broken on this box, and upgrading it is the wrong fix.

`python -m build` pulls an always-current hatchling that emits
`Metadata-Version: 2.5`. The global twine validates with `packaging` 24.2, which
tops out at 2.4, so the upload dies on `InvalidDistribution`. The two halves read
different toolchains and only one of them moves.

`pip install -U packaging` looks like the fix and is not. Three installed
packages cap that interpreter — `langchain-core` (`<25`), `streamlit` (`<25`),
and `inference-gpu` (`~=24.0`) — so the upgrade breaks three working packages to
satisfy a release tool. `uvx` resolves twine and its `packaging` in a throwaway
environment and leaves the global interpreter alone.

`python -m build` is also simply absent from the project venv. Use
`uvx --from build pyproject-build`.

⚠ **This failure does not look like a tooling failure.** It arrives during
`upload` and reads as a credentials or permissions problem, which sends you to
`~/.pypirc` instead of to the invocation. Measured 2026-09-01: a hypothesis about
a project-scoped token was built on a real clue and was wrong. The token was
fine. The invocation was wrong. **Check which command you ran before you check
your credentials.**

⚠ This box has several shells. A command that works in one fails in another —
`cd /d` is cmd.exe only. Note the shell for anything you record here.

## `twine check` is the load-bearing half

Run it as a gate before `upload`, never after. Without it the metadata failure is
discovered *during* upload, which can leave the wheel on PyPI and the sdist not.
A half-published version cannot be re-uploaded.

## The build reads your working tree, not `HEAD`

⚠⚠ Hatchling builds from the filesystem. Uncommitted edits to tracked files ship
with their edits. **A file that is untracked *and* unignored also ships.** This
is still true and is why step 6 cleans the tree first.

What changed on 2026-09-01 is the *default*.
`[tool.hatch.build.targets.sdist]` now declares an allowlist, so a path is out
unless it is named. Before that there was no sdist target at all, and contents
fell back to hatchling's default selection. That default does honor `.gitignore`
(`ignore-vcs` defaults to false), which is why `.claude/` never reached a
published sdist — but it made `.gitignore` the only thing standing between a
working-tree file and PyPI.

**The old protection was incidental. The new one is declared.** Verified by
building against planted decoys — a `.env`, a `*.bak-*`, and a `*.local.toml`:
none reached the artifact.

That this mattered is not hypothetical. Cutting 0.2.2 required stashing
`configs/jdocmunch.toml.bak-20260816-reconcile`, and the same stash turned out to
be the only thing keeping a live `BRAVE_API_KEY` — sitting uncommitted in
`configs/brave-search.toml` — out of the upload.

`tests/test_no_inline_credentials.py` catches that case one step earlier, where
the fix is free: a real secret in a tracked config fails the suite before any
build runs. Real credentials belong in `configs/*.local.toml`, which is
gitignored.

Inspecting the sdist is still cheap, and an allowlist only protects paths it was
told about:

```bash
tar tzf dist/*X.Y.Z*.tar.gz | grep -iE '\.claude|\.bak|\.env|secret' || echo "clean"
```

## Stale artifacts in `dist/`

`dist/` is gitignored and accumulates. A glob upload picks up whatever is still
there — `twine upload dist/*` with an old build present tries to re-upload a
published version and fails the whole command. Clear `dist/` in step 6.

## MCP registry

`server.json` exists as of 2026-09-01. Whether anything has been **published** is
a live fact - query it, never quote it from here:

```bash
curl -s 'https://registry.modelcontextprotocol.io/v0/servers?search=jmunch&limit=100' -o reg.json
```

⚠⚠ `&limit=100` is load-bearing; the default response is a page, not the set.

⚠⚠ Rows are nested `{server: {...}, _meta: {...}}` - `name` and `version` live
under `server`, `isLatest` under `_meta`. A flat `.name` read returns zero rows
on a perfectly good publish. Before trusting a zero, run the same query against
`search=jcodemunch`; a non-zero there proves the query shape works.

The publish is **typed by a human**, not run by an agent: the device flow blocks
on a browser and the JWT lives five minutes, so login and publish must be one
command run from the repo root.

- cmd.exe:

  ```
  cd /d C:\MCPs\jmunch-mcp && "C:\Users\j\mcp-publisher.exe" login github && "C:\Users\j\mcp-publisher.exe" publish
  ```

- PowerShell:

  ```
  cd C:\MCPs\jmunch-mcp; & "C:\Users\j\mcp-publisher.exe" login github; & "C:\Users\j\mcp-publisher.exe" publish
  ```

⚠ Literal paths only - no `~`, no `%USERPROFILE%`, no `$env:USERPROFILE`. Each
has failed at this prompt mid-release. `login` alone is invalid; the auth method
is a required argument.
