# Releasing jmunch-mcp

This repo has **no CI**. There are no workflows, so nothing here enforces any of
the steps below. The local suite is the only gate that exists. Read this file
before every release.

The sibling repos (jcodemunch, jdocmunch, jdatamunch) are covered by the shared
`release` skill. This repo is not in that skill's scope but shares its build
stack, so it carries the same hazards with none of the automation.

## Steps

1. **Bump the version.** There is one pin site: `pyproject.toml`. Confirm it is
   still the only one:

   ```bash
   grep -rn "<old-version>" --include=*.json --include=*.toml --include=*.lock .
   ```

   The list grows. Do not trust this count without running the command.

2. **Date the CHANGELOG.** Change `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`.

3. **Run the full suite.** `PYTHONPATH=src` is required. Without it the
   installed package shadows `src/` and you test the wrong code.

   ```bash
   PYTHONPATH=src python -m pytest tests/ -q
   ```

   Read the **skip count**, not only the exit code. A jump in skips means the
   environment changed, not the code.

4. **Run the linter.** `uv run ruff check src/`. It is not wired to anything, so
   it fails silently by never running. Compare against the previous release
   rather than expecting zero — this repo carries pre-existing findings.

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
with their edits. **A file that is untracked *and* unignored also ships.**

`pyproject.toml` declares only `[tool.hatch.build.targets.wheel]`. There is no
sdist target block, so sdist contents fall back to hatchling's default selection.
That default does honor `.gitignore` (`ignore-vcs` defaults to false), which is
why `.claude/` — line 36 of `.gitignore` — has stayed out of every published
sdist. Verified 2026-09-01 by unpacking the 0.2.1 sdist from PyPI: 94 files, no
`.claude/`, no `.bak`, no credentials.

**The protection is incidental, not declared.** `.gitignore` is doing it. Anything
`.gitignore` does not name is in the sdist.

This is not hypothetical. Cutting 0.2.2 required stashing
`configs/jdocmunch.toml.bak-20260816-reconcile` — untracked, unignored, and
otherwise bound for the artifact — along with two modified `configs/*.toml`
carrying local-only workarounds.

Until an explicit sdist target block exists, **inspect the sdist before every
upload**:

```bash
tar tzf dist/*X.Y.Z*.tar.gz | grep -iE '\.claude|\.bak|\.env|secret' || echo "clean"
```

## Stale artifacts in `dist/`

`dist/` is gitignored and accumulates. A glob upload picks up whatever is still
there — `twine upload dist/*` with an old build present tries to re-upload a
published version and fails the whole command. Clear `dist/` in step 6.

## Not in the MCP registry

jmunch-mcp has no `server.json` and has never been published to
`registry.modelcontextprotocol.io`. Verified 2026-09-01: the query returns zero
rows, and the same query shape returns 57 rows for jcodemunch, so the zero is
real rather than a parse error.

The sibling repos' registry-publish step does not apply here. Adding it is
separate work, not part of a release.

⚠ Query the registry; never quote a publication state from a document. The claim
expires the moment someone publishes.
