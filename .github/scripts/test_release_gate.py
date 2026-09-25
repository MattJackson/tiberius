#!/usr/bin/env python3
"""Tests for release_gate.py. Offline: a throwaway git repo holds a two-crate
workspace shaped like tiberius (tiberius -> tiberius-macros, path + version),
and a file:// sparse index plays the registry.

Run: python3 .github/scripts/test_release_gate.py -v
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_gate as rg  # noqa: E402

ENV = dict(os.environ, CARGO_NET_OFFLINE="true", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, env=ENV, check=True, text=True, capture_output=True).stdout.strip()


class Fixture:
    """Workspace repo + fake registry."""

    def __init__(self, tmp: Path):
        self.repo = tmp / "repo"
        self.index = tmp / "index"
        self.dl = tmp / "dl"
        for d in (self.repo, self.index, self.dl):
            d.mkdir()
        (self.index / "config.json").write_text(
            json.dumps({"dl": f"file://{self.dl}/{{crate}}-{{version}}.crate", "api": "x"})
        )
        sh(self.repo, "git", "init", "-q", "-b", "dev")
        self.write_ws("0.13.0", "0.1.0", macros_req="0.1.0", changelog=["0.13.0"])
        self.commit("base")

    # -- workspace --------------------------------------------------------

    def write_ws(self, tib, mac, macros_req=None, changelog=(), macros_body="// macros\n"):
        r = self.repo
        (r / "src").mkdir(exist_ok=True)
        (r / "tiberius-macros" / "src").mkdir(parents=True, exist_ok=True)
        req = macros_req or mac
        (r / "Cargo.toml").write_text(
            f'[package]\nname = "tiberius"\nversion = "{tib}"\nedition = "2021"\n'
            'license = "MIT"\ndescription = "t"\n\n[workspace]\nmembers = ["tiberius-macros"]\n\n'
            f'[dependencies]\ntiberius-macros = {{ path = "tiberius-macros", version = "{req}" }}\n'
        )
        (r / "src" / "lib.rs").write_text("pub fn f() {}\n")
        (r / "tiberius-macros" / "Cargo.toml").write_text(
            f'[package]\nname = "tiberius-macros"\nversion = "{mac}"\nedition = "2021"\n'
            'license = "MIT"\ndescription = "t"\n'
        )
        (r / "tiberius-macros" / "src" / "lib.rs").write_text(macros_body)
        (r / "CHANGELOG.md").write_text(
            "# Changes\n\n" + "".join(f"## Version {v}\n\n- x\n\n" for v in changelog)
        )
        (r / ".gitignore").write_text("target\nCargo.lock\n")

    def commit(self, msg):
        sh(self.repo, "git", "add", "-A")
        # Isolate from the developer's global hooks / signing config.
        sh(self.repo, "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
           "commit", "-q", "-m", msg)
        return sh(self.repo, "git", "rev-parse", "HEAD")

    # -- registry ---------------------------------------------------------

    def publish(self, name, version, vcs_sha="0" * 40, yanked=False):
        """Package the current working tree state of `name` into the fake registry."""
        pkg = next(p for p in rg.workspace_packages(self.repo) if p.name == name)
        files = sh(self.repo, "cargo", "package", "--list", "--allow-dirty", "-p", name).splitlines()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(rel, data):
                info = tarfile.TarInfo(f"{name}-{version}/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            for rel in files:
                if rel == "Cargo.toml.orig":
                    add(rel, pkg.manifest.read_bytes())
                elif rel == ".cargo_vcs_info.json":
                    add(rel, json.dumps({"git": {"sha1": vcs_sha}}).encode())
                elif rel in ("Cargo.toml", "Cargo.lock"):
                    add(rel, b"# generated, differs every build\n" + os.urandom(8).hex().encode())
                else:
                    add(rel, (pkg.dir / rel).read_bytes())
        data = buf.getvalue()
        (self.dl / f"{name}-{version}.crate").write_bytes(data)
        path = self.index / rg.index_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"name": name, "vers": version, "cksum": hashlib.sha256(data).hexdigest(),
                 "yanked": yanked, "deps": [], "features": {}}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return data

    def registry(self):
        return rg.Registry(f"file://{self.index}/")

    def gate(self, sha="HEAD", qa_runs=None, qa_ref="dev"):
        sha = sh(self.repo, "git", "rev-parse", sha)
        return rg.evaluate(self.repo, sha, self.registry(), "tiberius", qa_runs, qa_ref)


def run_for(sha, event="merge_group", conclusion="success"):
    return {"head_sha": sha, "html_url": f"https://example/runs/{sha[:7]}", "event": event,
            "conclusion": conclusion}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fx = Fixture(self.tmp)
        # Published state == the base commit, like tiberius 0.13.0 / macros 0.1.0.
        self.fx.publish("tiberius-macros", "0.1.0")
        self.fx.publish("tiberius", "0.13.0")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def assertError(self, result, needle):
        self.assertTrue(any(needle in e for e in result.errors),
                        f"expected an error containing {needle!r}, got {result.errors}")

    def bump(self, tib="0.13.1", mac="0.1.0", changelog=("0.13.1", "0.13.0"), **kw):
        self.fx.write_ws(tib, mac, changelog=changelog, **kw)
        return self.fx.commit(f"release {tib}")

    # -- the no-release rule --------------------------------------------------

    def test_nothing_to_release_fails(self):
        r = self.fx.gate()
        self.assertEqual(r.errors, [rg.NO_RELEASE_MSG])
        self.assertEqual({c.action for c in r.crates}, {"unchanged"})

    def test_unbumped_changed_contents_fail(self):
        (self.fx.repo / "src" / "lib.rs").write_text("pub fn g() {}\n")
        self.fx.commit("change")
        r = self.fx.gate()
        self.assertError(r, "tiberius: contents differ from the published tiberius 0.13.0")
        self.assertError(r, "modified src/lib.rs")
        self.assertIn(rg.NO_RELEASE_MSG, r.errors)

    def test_non_package_files_do_not_count(self):
        # Excluded by .gitignore -> not in `cargo package --list`.
        (self.fx.repo / "target").mkdir(exist_ok=True)
        (self.fx.repo / "target" / "junk").write_text("x")
        self.assertEqual(self.fx.gate().errors, [rg.NO_RELEASE_MSG])

    # -- the happy path (mirrors fixes/stacked: tiberius 0.13.1, macros unchanged) --

    def test_tiberius_only_release_passes(self):
        sha = self.bump()
        r = self.fx.gate(qa_runs=[run_for(sha)])
        self.assertEqual(r.errors, [])
        self.assertEqual([c.name for c in r.publish], ["tiberius"])
        self.assertEqual(r.publish[0].tag, "v0.13.1")
        self.assertEqual(r.qa_run["head_sha"], sha)

    def test_both_crates_release_in_dependency_order(self):
        sha = self.bump(mac="0.1.1", macros_req="0.1.1", macros_body="// new\n")
        r = self.fx.gate(qa_runs=[run_for(sha)])
        self.assertEqual(r.errors, [])
        self.assertEqual([c.name for c in r.publish], ["tiberius-macros", "tiberius"])
        self.assertEqual(r.publish[0].tag, "tiberius-macros-v0.1.1")

    def test_prerelease_bump_passes(self):
        sha = self.bump(tib="0.13.1-proof.1", changelog=("0.13.1-proof.1",))
        self.assertEqual(self.fx.gate(qa_runs=[run_for(sha)]).errors, [])

    # -- synthetic failures ----------------------------------------------------

    def test_lower_version_fails(self):
        self.bump(tib="0.12.9", changelog=("0.12.9",))
        self.assertError(self.fx.gate(), "tiberius: version 0.12.9 is lower than the latest published 0.13.0")

    def test_macros_changed_without_bump_fails(self):
        self.bump(macros_body="// changed\n")
        r = self.fx.gate()
        self.assertError(r, "tiberius-macros: contents differ from the published tiberius-macros 0.1.0")
        self.assertError(r, "modified src/lib.rs")

    def test_missing_changelog_heading_fails(self):
        self.bump(changelog=("0.13.0",))
        self.assertError(self.fx.gate(), "missing a `## Version 0.13.1` heading")

    def test_tag_on_other_commit_fails(self):
        base = sh(self.fx.repo, "git", "rev-parse", "HEAD")
        sh(self.fx.repo, "git", "tag", "v0.13.1", base)
        self.bump()
        self.assertError(self.fx.gate(), "tag v0.13.1 already exists on")

    def test_tag_on_same_commit_is_ok(self):
        sha = self.bump()
        sh(self.fx.repo, "git", "tag", "v0.13.1", sha)
        self.assertEqual(self.fx.gate(qa_runs=[run_for(sha)]).errors, [])

    def test_invalid_semver_fails(self):
        # cargo itself rejects most invalid versions, so inject one.
        real = rg.workspace_packages

        def patched(root):
            pkgs = real(root)
            for p in pkgs:
                if p.name == "tiberius":
                    p.version_text = "0.13"
            return pkgs

        self.bump()
        with mock.patch.object(rg, "workspace_packages", patched):
            self.assertError(self.fx.gate(), "tiberius: version '0.13' in Cargo.toml is not valid semver")

    def test_unpublished_macros_dependency_fails(self):
        # macros 0.0.9 cannot be released (lower than 0.1.0), yet tiberius needs it.
        self.bump(mac="0.0.9", macros_req="=0.0.9")
        r = self.fx.gate()
        self.assertError(r, "tiberius requires tiberius-macros =0.0.9, which is not published and is not being released now")

    def test_tree_matching_no_green_qa_run_fails(self):
        sha = self.bump()
        base = sh(self.fx.repo, "git", "rev-parse", "HEAD~1")
        cases = {
            "no runs": [],
            "only an older tree": [run_for(base)],
            "failed run": [run_for(sha, conclusion="failure")],
            "pull_request shim run": [run_for(sha, event="pull_request")],
        }
        for label, runs in cases.items():
            with self.subTest(label):
                self.assertError(self.fx.gate(qa_runs=runs), "does not match any commit on dev with a successful QA run")

    def test_tree_identity_across_different_shas(self):
        # dev commit D is QA'd; the release PR merge commit M differs but has the same tree.
        d = self.bump()
        sh(self.fx.repo, "git", "checkout", "-q", "-b", "main", "HEAD~1")
        sh(self.fx.repo, "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
           "merge", "-q", "--no-ff", "-m", "Merge dev", "dev")
        m = sh(self.fx.repo, "git", "rev-parse", "HEAD")
        self.assertNotEqual(m, d)
        r = self.fx.gate(sha=m, qa_runs=[run_for(d)])
        self.assertEqual(r.errors, [])

    def test_qa_run_not_on_dev_fails(self):
        sh(self.fx.repo, "git", "checkout", "-q", "-b", "side")
        sha = self.bump()
        sh(self.fx.repo, "git", "checkout", "-q", "dev")
        r = self.fx.gate(sha=sha, qa_runs=[run_for(sha, event="workflow_dispatch")])
        self.assertError(r, "does not match any commit on dev")

    # -- retry ----------------------------------------------------------------

    def test_retry_after_partial_publish_resumes(self):
        sha = self.bump()
        self.fx.publish("tiberius", "0.13.1", vcs_sha=sha)
        r = self.fx.gate(qa_runs=[run_for(sha)])
        self.assertEqual(r.errors, [])
        self.assertEqual([c.name for c in r.resume], ["tiberius"])
        self.assertEqual(r.publish, [])


class VerifyTests(unittest.TestCase):
    def test_verify_cksum(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            fx = Fixture(tmp)
            data = fx.publish("tiberius", "0.13.0")
            crate = tmp / "t.crate"
            crate.write_bytes(data)
            base = ["verify", "--crate", "tiberius", "--version", "0.13.0",
                    "--index-url", f"file://{fx.index}/", "--timeout", "0"]
            self.assertEqual(rg.main(base + ["--crate-file", str(crate)]), 0)
            crate.write_bytes(data + b"x")
            self.assertEqual(rg.main(base + ["--crate-file", str(crate)]), 1)
            missing = [a if a != "0.13.0" else "9.9.9" for a in base]
            self.assertEqual(rg.main(missing + ["--crate-file", str(crate), "--interval", "0"]), 1)
        finally:
            shutil.rmtree(tmp)


class SemverTests(unittest.TestCase):
    def test_ordering(self):
        v = rg.Version.parse
        self.assertLess(v("0.13.0"), v("0.13.1-proof.1"))
        self.assertLess(v("0.13.1-proof.1"), v("0.13.1"))
        self.assertLess(v("1.0.0-alpha.2"), v("1.0.0-alpha.10"))
        self.assertLess(v("1.0.0-alpha"), v("1.0.0-alpha.1"))
        self.assertLess(v("1.0.0-9"), v("1.0.0-a"))

    def test_invalid(self):
        for bad in ("0.13", "01.2.3", "1.2.3-", "v1.2.3", "1.2.3.4", ""):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    rg.Version.parse(bad)

    def test_req(self):
        v = rg.Version.parse
        cases = [
            ("^0.1.0", "0.1.5", True), ("0.1.0", "0.2.0", False), ("^0.0.3", "0.0.4", False),
            ("^1.2", "1.9.0", True), ("^1.2", "2.0.0", False), ("~0.1.2", "0.1.9", True),
            ("~0.1.2", "0.2.0", False), ("=0.1.0", "0.1.1", False), (">=0.1, <0.3", "0.2.9", True),
            (">=0.1, <0.3", "0.3.0", False), ("0.1.*", "0.1.7", True), ("*", "3.0.0", True),
            ("^0.1.0", "0.1.1-rc.1", False), ("^0.1.1-rc.1", "0.1.1-rc.2", True),
            ("<0.2", "0.1.9", True), ("<=0.2", "0.2.5", True), (">0.2", "0.2.5", False),
        ]
        for req, ver, want in cases:
            with self.subTest(req=req, ver=ver):
                self.assertEqual(rg.req_matches(req, v(ver)), want)


if __name__ == "__main__":
    unittest.main()
