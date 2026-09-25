# Contributing

## Branches and pull requests

- `dev` is the default branch. Open pull requests against `dev`.
- Every push to a PR runs the fast CI tier (`.github/workflows/ci.yml`):
  rustfmt, clippy, unit tests for the main feature sets, MSRV, docs,
  cargo-deny, gitleaks, an advisory semver check and a SQL Server 2022 smoke
  test. The required check is `ci-ok`.
- Approved PRs land through the merge queue. The queue runs the heavy tier
  (`.github/workflows/qa.yml`) on the exact commit that will land: the full
  Linux SQL Server matrix, macOS, Windows integrated auth, and a strict
  semver check against the latest crates.io release. A red run removes the PR
  from the queue. The required check is `qa-ok`.
- A breaking API change must bump the version on `dev` in the same PR (for
  0.x, a minor bump), otherwise the strict semver check fails.

## Releasing

`main` always equals the latest crates.io release. Changes that bump no
version may still land on `main` when they touch no crate code (CI, docs,
tests); the release gate passes them as a no-op. Crate code (`src/`,
`build.rs`, the manifests, `tiberius-macros/src/`) only reaches `main`
through a release.

1. On `dev`, land a PR that bumps `version` in `Cargo.toml` (and in
   `tiberius-macros/Cargo.toml` if the macros changed) and adds a
   `## Version X.Y.Z` section to `CHANGELOG.md`. Wait for the merge queue
   to finish; that QA run is what the release is checked against.
2. Cut a release branch: a single commit on top of `main` whose tree is
   exactly `dev`'s tree.

   ```sh
   git fetch origin
   git switch -c release/X.Y.Z origin/main
   git read-tree -u --reset origin/dev   # index + worktree = dev's tree
   git commit -m "chore: release X.Y.Z"
   git push origin release/X.Y.Z
   ```

   `main` only accepts rebase or squash merges, which rewrite commits, so
   `dev` and `main` never share history after the first release. A branch
   built this way always rebases cleanly, and it lands exactly the tree QA
   saw.
3. Open a PR from `release/X.Y.Z` into `main`. The `release gate` check
   verifies:
   - the version bump and the changelog heading;
   - that the tag is free;
   - that the branch contains the current `main` tip;
   - that the tree is identical to a `dev` commit that passed QA.
4. Merge it with **Rebase and merge** (or squash). Publishing is automatic:
   `.github/workflows/release.yml` publishes to crates.io with Trusted
   Publishing, verifies the index, then tags `vX.Y.Z` and creates the
   GitHub Release.

If a release fails part-way, re-run the failed jobs, or use the Release
workflow's "Run workflow" button on `main` with dry run off. Crates already
published from that commit are skipped.
