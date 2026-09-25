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

`main` always equals the latest crates.io release.

1. On `dev`, land a PR that bumps `version` in `Cargo.toml` (and in
   `tiberius-macros/Cargo.toml` if the macros changed) and adds a
   `## Version X.Y.Z` section to `CHANGELOG.md`.
2. Open a PR from `dev` into `main`. The `release gate` check verifies the
   version bump, the changelog heading, that the tag is free, and that the
   tree is identical to a `dev` commit that passed QA.
3. Merge it. Publishing is automatic: `.github/workflows/release.yml`
   publishes to crates.io with Trusted Publishing, verifies the index, then
   tags `vX.Y.Z` and creates the GitHub Release.

If a release fails part-way, re-run it with the Release workflow's
"Run workflow" button on `main` (dry run off). Crates already published from
that commit are skipped.
