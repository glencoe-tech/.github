"""Every rule is exercised passing and failing, against real Git repositories in a temp dir."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docs_standard as ds  # noqa: E402

TODAY = dt.date(2026, 11, 17)


class Repo:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def write(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, files: dict[str, str] | None = None, remove: list[str] | None = None) -> str:
        self.write(files or {})
        for path in remove or []:
            self.git("rm", "-q", path)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        return self.git("rev-parse", "HEAD").strip()

    def close(self) -> None:
        self._tmp.cleanup()


def codes(findings: list[ds.Finding]) -> list[str]:
    return sorted(f.code for f in findings)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fresh()

    def fresh(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.close)

    def config(self, raw: dict | list | None = None) -> dict:
        if raw is not None:
            self.repo.write({".github/docs-standard.json": json.dumps(raw)})
        return ds.load_config(self.repo.root, ".github/docs-standard.json")

    def scan(self, raw: dict | None = None) -> list[ds.Finding]:
        config = self.config(raw)
        self.repo.commit()
        return ds.run(self.repo.root, None, None, config, TODAY)

    def pr(self, before: dict[str, str], after: dict[str, str], body: str | None = "",
           raw: dict | None = None, remove: list[str] | None = None) -> list[ds.Finding]:
        config = self.config(raw)
        base = self.repo.commit(before)
        self.repo.commit(after, remove)
        return ds.run(self.repo.root, base, body, config, TODAY)


class GlobTests(unittest.TestCase):
    def test_patterns(self) -> None:
        self.assertTrue(ds.matches("docs/a/b.md", ["docs/**"]))
        self.assertTrue(ds.matches("README.md", ["**/README.md"]))
        self.assertTrue(ds.matches("x/README.md", ["**/README.md"]))
        self.assertFalse(ds.matches("x/y.md", ["*.md"]))
        self.assertTrue(ds.matches("a1.md", ["a?.md"]))
        self.assertTrue(ds.matches("ops/RUNBOOK.md", ["**/*[Rr][Uu][Nn][Bb][Oo][Oo][Kk]*.md"]))
        self.assertTrue(ds.matches("a[b.md", ["a[b.md"]))
        self.assertTrue(ds.matches("src/x/y.py", ["src**"]))


class ConfigTests(Base):
    def test_defaults_without_file(self) -> None:
        self.assertEqual(self.config()["doc_paths"], ds.DEFAULT_DOC_PATHS)

    def test_rejections(self) -> None:
        for raw, needle in (
            ([], "object"),
            ({"surprise": 1}, "unknown keys"),
            ({"high_impact": [{"id": "x"}]}, "high_impact"),
            ({"exceptions": [{"code": "D101"}]}, "exceptions need"),
        ):
            with self.subTest(needle=needle), self.assertRaisesRegex(ds.ConfigError, needle):
                self.config(raw)

    def test_bad_expiry_date(self) -> None:
        item = {k: "x" for k in ds.EXCEPTION_KEYS}
        with self.assertRaises(ValueError):
            self.config({"exceptions": [item]})


class LinkTests(Base):
    def test_valid_links_pass(self) -> None:
        self.repo.write({
            "docs/guide.md": "# Guide\n\n## Set up — fast\n\n## Set up — fast\n<a name=\"Custom\"></a>\n",
            "docs/sub/x.py": "",
            "README.md": (
                "[g](docs/guide.md) [h](docs/guide.md#set-up--fast) [h2](docs/guide.md#set-up--fast-1)\n"
                "[c](docs/guide.md#custom) [dir](docs/sub/) [abs](/docs/guide.md) [self](#readme)\n"
                "[ext](https://example.com) [mail](mailto:a@b) [proto](//cdn.example) [code](docs/sub/x.py#L1)\n"
                "[tpl](<board url>) [space](a b) ![img](docs/guide.md \"title\") [q](docs/guide.md?plain=1)\n"
                "[ref]: docs/guide.md\n`[inline](missing.md)`\n```\n[fenced](missing.md)\n```\n# Readme\n"
            ),
        })
        self.assertEqual(self.scan(), [])

    def test_broken_target_and_anchor(self) -> None:
        self.repo.write({
            "docs/guide.md": "# Guide\n",
            "README.md": "[a](docs/missing.md)\n[b](docs/guide.md#nope)\n[c](#nowhere)\n[r]: ./gone.md\n",
        })
        found = self.scan()
        self.assertEqual(codes(found), ["D101"] * 4)
        self.assertEqual([f.line for f in found], [1, 2, 3, 4])

    def test_ignore_skips_scan(self) -> None:
        self.repo.write({"import/x.md": "[a](missing.md)\n"})
        self.assertEqual(self.scan({"ignore": ["import/**"]}), [])

    def test_templates_are_always_ignored(self) -> None:
        self.repo.write({"work/templates/RUNBOOK.md": "[x](link)\n", ".github/pull_request_template.md": "[y](z)\n"})
        self.assertEqual(self.scan({"ignore": []}), [])

    def test_pr_rechecks_docs_that_named_a_removed_file(self) -> None:
        found = self.pr(
            {"docs/old.md": "# Old\n", "README.md": "[o](docs/old.md)\n", "docs/other.md": "plain\n"},
            {}, remove=["docs/old.md"],
        )
        self.assertEqual([(f.code, f.path) for f in found], [("D101", "README.md")])

    def test_pr_rename_is_delete_plus_add(self) -> None:
        found = self.pr({"docs/old.md": "# Old\n", "README.md": "[o](docs/old.md)\n"}, {}, remove=[])
        self.assertEqual(found, [])
        base = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("mv", "docs/old.md", "docs/new.md")
        self.repo.commit()
        changes = ds.changed_files(self.repo.root, base)
        self.assertEqual(changes, {"docs/old.md": "D", "docs/new.md": "A"})
        found = ds.run(self.repo.root, base, "", self.config(), TODAY)
        self.assertEqual([(f.code, f.path) for f in found], [("D101", "README.md")])

    def test_copy_and_type_change_statuses(self) -> None:
        base = self.repo.commit({"a.txt": "x\n" * 50})
        self.repo.git("mv", "a.txt", "b.txt")
        self.repo.write({"a.txt": "x\n" * 50})
        self.repo.commit()
        with mock.patch.object(ds, "git", return_value="C100\0a.txt\0c.txt\0T\0d.txt\0"):
            self.assertEqual(ds.changed_files(self.repo.root, base), {"c.txt": "A", "d.txt": "M"})


class MetadataTests(Base):
    def test_scan_reports_missing_fields_and_bad_status(self) -> None:
        self.repo.write({
            "docs/adr/0001-one.md": "# ADR\n\n**Status:** Accepted.\n",
            "docs/adr/0002-two.md": "# ADR\n\nNo fields.\n",
            "docs/adr/0003-three.md": "---\nstatus: Pending\n---\n# ADR\n",
            "docs/adr/0004-four.md": "---\nStatus: proposed\ntitle: x\n---\n",
            "docs/adr/README.md": "# Index\n",
            "ops/DB-RUNBOOK.md": "# Runbook\n- **Owner:** @glencoe-tech/platform-team\n",
            "ops/cache-runbook.md": "# Runbook\n",
        })
        found = self.scan()
        self.assertEqual(
            [(f.code, f.path) for f in found],
            [("D102", "docs/adr/0002-two.md"), ("D102", "docs/adr/0003-three.md"), ("D102", "ops/cache-runbook.md")],
        )

    def test_pr_checks_only_touched_governed_docs(self) -> None:
        found = self.pr({"ops/a-runbook.md": "# A\n", "README.md": "x\n"}, {"README.md": "y\n"})
        self.assertEqual(found, [])
        found = self.pr({}, {"ops/a-runbook.md": "# A changed\n"})
        self.assertEqual(codes(found), ["D102"])

    def test_field_parser(self) -> None:
        self.assertEqual(ds.fields("**Owner:** team\n**Version:** 1.2.0  \n")["version"], "1.2.0")
        self.assertEqual(ds.fields("---\nowner: a\nbroken\n")["owner"], "a")


class DecisionIdTests(Base):
    def test_duplicates_in_one_directory(self) -> None:
        self.repo.write({
            "docs/adr/0001-a.md": "**Status:** Accepted\n",
            "docs/adr/0001-b.md": "**Status:** Accepted\n",
            "other/adr/0001-a.md": "**Status:** Accepted\n",
        })
        found = self.scan()
        self.assertEqual([(f.code, f.path) for f in found], [("D103", "docs/adr/0001-b.md")])

    def test_pr_reports_the_file_it_added(self) -> None:
        found = self.pr({"docs/adr/0002-zeta.md": "**Status:** Accepted\n"}, {"docs/adr/0002-alpha.md": "**Status:** Proposed\n"})
        self.assertEqual([(f.code, f.path) for f in found], [("D103", "docs/adr/0002-alpha.md")])

    def test_custom_pattern(self) -> None:
        self.repo.write({"docs/AX-RFC-0004-a.md": "", "docs/AX-RFC-0004-b.md": "", "docs/notes.md": ""})
        raw = {"decision_records": [{"glob": "docs/AX-RFC-*.md", "id": r"^AX-RFC-(\d{4})-"}]}
        self.assertEqual(codes(self.scan(raw)), ["D103"])


class ImpactTests(Base):
    RULES = {"high_impact": [{"id": "schema", "paths": ["migrations/**"], "docs": ["docs/data/**"]}]}

    def change(self, body: str | None, extra: dict[str, str] | None = None) -> list[ds.Finding]:
        after = {"migrations/002.sql": "alter;\n", **(extra or {})}
        return self.pr({"migrations/001.sql": "create;\n"}, after, body, self.RULES)

    def test_trivial_change_needs_nothing(self) -> None:
        self.assertEqual(self.pr({"src/a.py": "x = 1\n"}, {"src/a.py": "x = 2\n"}, "", self.RULES), [])

    def test_high_impact_without_docs_or_declaration(self) -> None:
        found = self.change("Adds a column.")
        self.assertEqual([(f.code, f.path) for f in found], [("D104", "migrations/002.sql")])

    def test_satisfied_by_mapped_docs(self) -> None:
        self.assertEqual(self.change("", {"docs/data/schema.md": "# Schema\n"}), [])

    def test_satisfied_by_valid_declarations(self) -> None:
        for body in (
            "Documentation impact: none — column is internal and unread",
            "- **Documentation impact:** follow-up glencoe-tech/axentra-core#12",
            "Documentation impact: follow-up https://github.com/o/r/issues/3",
        ):
            with self.subTest(body=body):
                self.fresh()
                self.assertEqual(self.change(body), [])

    def test_invalid_declarations(self) -> None:
        for body in ("Documentation impact: none", "Documentation impact: updated", "Documentation impact: follow-up later"):
            with self.subTest(body=body):
                self.fresh()
                self.assertEqual(codes(self.change(body)), ["D104"])

    def test_updated_with_other_docs_passes(self) -> None:
        self.assertEqual(self.change("Documentation impact: updated README", {"README.md": "b\n"}), [])

    def test_unfilled_template_line_on_a_trivial_change_is_ignored(self) -> None:
        found = self.pr({"src/a.py": "x = 1\n"}, {"src/a.py": "x = 2\n"}, "Documentation impact: none — ", self.RULES)
        self.assertEqual(found, [])

    def test_workflow_files_are_not_documentation(self) -> None:
        raw = {"high_impact": [{"id": "ci", "paths": [".github/workflows/**"], "docs": ["docs/ci/**"]}]}
        found = self.pr({}, {".github/workflows/ci.yml": "on: push\n"}, "Documentation impact: updated", raw)
        self.assertEqual([(f.code, f.path) for f in found], [("D104", "PR description")])

    def test_no_pull_request_context_skips(self) -> None:
        self.assertEqual(self.change(None), [])


class ProtectedTests(Base):
    RAW = {"protected": ["docs/STANDARD.md"]}

    def test_version_must_change(self) -> None:
        before = {"docs/STANDARD.md": "**Version:** 1.0.0\n\nText.\n"}
        self.assertEqual(codes(self.pr(before, {"docs/STANDARD.md": "**Version:** 1.0.0\n\nNew.\n"}, "", self.RAW)), ["D106"])

    def test_version_bump_passes_and_new_doc_passes(self) -> None:
        before = {"docs/STANDARD.md": "**Version:** 1.0.0\n"}
        self.assertEqual(self.pr(before, {"docs/STANDARD.md": "**Version:** 1.1.0\n"}, "", self.RAW), [])
        self.assertEqual(self.pr({}, {"docs/OTHER.md": "x\n"}, "", {"protected": ["docs/OTHER.md"]}), [])

    def test_missing_version_and_deletion(self) -> None:
        self.assertEqual(codes(self.pr({"docs/STANDARD.md": "a\n"}, {"docs/STANDARD.md": "b\n"}, "", self.RAW)), ["D106"])
        self.assertEqual(codes(self.pr({}, {}, "", self.RAW, remove=["docs/STANDARD.md"])), ["D106"])

    def test_unreadable_base_counts_as_no_version(self) -> None:
        base = self.repo.commit({"docs/STANDARD.md": "**Version:** 1.0.0\n"})
        with mock.patch.object(ds, "git", side_effect=subprocess.CalledProcessError(128, "git")):
            found = ds.check_protected(self.repo.root, base, {"docs/STANDARD.md": "M"}, self.config(self.RAW))
        self.assertEqual(found, [])


class CommentTests(unittest.TestCase):
    def lines(self, text: str) -> list[tuple[int, str]]:
        return list(enumerate(text.splitlines(), 1))

    def test_commented_out_code(self) -> None:
        py = "# old = compute(x)\n# return old\nvalue = 1\n"
        self.assertEqual(codes(ds.check_comments("a.py", self.lines(py))), ["W201"])
        ts = "// const a = b();\n// if (a) {\nconst c = 1;\n"
        self.assertEqual(codes(ds.check_comments("a.ts", self.lines(ts))), ["W201"])
        single = "# x = 1\ny = 2\n"
        self.assertEqual(ds.check_comments("a.py", self.lines(single)), [])

    def test_prose_is_not_code(self) -> None:
        for prose in (
            "# if the token is missing we refuse,\n# return 401 rather than leak existence\nx = 1\n",
            "# KEYS[1] = bucket key\n# ARGV    = max_tokens, refill_rate (tokens/sec), now_seconds, cost\nx = 1\n",
            "# from cache after it is off. `*` is distinct\n# from an explicit empty set.\nx = 1\n",
            "# Error = the guard caught it;\n# IntegrityError = the unique index caught the racing insert.\nx = 1\n",
            "#   Temporal UI:  http://localhost:8233\n#   Grafana:      http://localhost:3300\nx = 1\n",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(ds.check_comments("a.py", self.lines(prose)), [])
        for code in ("for item in items:", "if (a) {", "else {", "def f(x):", "class A:", "let x = 1",
                     "import os", "from a import b", "await save(x)", "print(x)", "console.log(x)",
                     "foo.bar(x);", "});", "items.map((x) => {", "total += 1"):
            with self.subTest(code=code):
                self.assertTrue(ds.code_like(code))

    def test_history_comment(self) -> None:
        text = "// 2026-10-01: fixed the retry loop\nretry();\n# changed by @dev\nz = 1\n"
        found = ds.check_comments("a.go", self.lines(text)[:2]) + ds.check_comments("a.py", self.lines(text)[2:])
        self.assertEqual(codes(found), ["W202", "W202"])

    def test_narrating_comment(self) -> None:
        self.assertEqual(codes(ds.check_comments("a.py", self.lines("# get the user\nuser = get_user(uid)\n"))), ["W203"])
        self.assertEqual(codes(ds.check_comments("a.ts", self.lines("// Save user\nsaveUser(u)\n"))), ["W203"])

    def test_intent_comments_survive(self) -> None:
        for text in (
            "# get user first because the tenant filter needs it\nuser = get_user(uid)\n",
            "# Retry: the upstream returns 504 under load\nretry()\n",
            "# user\nuser = 1\n",
            "#!/usr/bin/env python3\nimport os\n",
            "#\nx = 1\n",
            "# get user\n# get user\n",
            "# ── Validate state ─────\nstate = validate_state(s)\n",
            "# The old code returned a hardcoded value; updated by scripts/release.py\nx = 1\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(ds.check_comments("a.py", self.lines(text)), [])

    def test_unknown_language_skipped(self) -> None:
        self.assertEqual(ds.check_comments("a.txt", [(1, "# x = 1"), (2, "# y = 2")]), [])

    def test_pr_reads_added_lines_only(self) -> None:
        repo = Repo()
        self.addCleanup(repo.close)
        base = repo.commit({"a.py": "# old = f(x)\n# return old\nx = 1\n"})
        repo.commit({"a.py": "# old = f(x)\n# return old\nx = 1\n# y = g(x)\n# return y\n"})
        found = ds.run(repo.root, base, "", ds.load_config(repo.root, "none.json"), TODAY)
        self.assertEqual([(f.code, f.line) for f in found], [("W201", 4)])


class NewDocTests(Base):
    def test_diary_and_sprawl_warnings(self) -> None:
        found = self.pr({}, {
            "docs/FIX_SUMMARY.md": "x\n", "notes/idea.md": "x\n", "docs/guide.md": "x\n",
            "src/README.md": "x\n", "docs/data.json": "{}",
        })
        self.assertEqual([(f.code, f.path) for f in found], [("W204", "docs/FIX_SUMMARY.md"), ("W204", "notes/idea.md")])

    def test_modified_docs_are_not_new(self) -> None:
        self.assertEqual(self.pr({"notes/idea.md": "a\n"}, {"notes/idea.md": "b\n"}), [])


class ExceptionTests(Base):
    def exception(self, **overrides: str) -> dict:
        item = {"code": "D101", "path": "README.md", "reason": "r", "owner": "@team", "risk": "low",
                "expires": "2026-12-31", "replacement": "fix the link"}
        return {**item, **overrides}

    def test_active_exception_suppresses_only_its_finding(self) -> None:
        self.repo.write({"README.md": "[a](gone.md)\n", "docs/x.md": "[b](gone.md)\n"})
        found = self.scan({"exceptions": [self.exception()]})
        self.assertEqual([(f.code, f.path) for f in found], [("D101", "docs/x.md")])

    def test_expired_and_overlong_exceptions_fail(self) -> None:
        self.repo.write({"README.md": "[a](gone.md)\n"})
        raw = {"exceptions": [self.exception(expires="2026-11-01"), self.exception(expires="2027-12-01")]}
        self.assertEqual(codes(self.scan(raw)), ["D101", "D107", "D107"])


class CliTests(Base):
    def call(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ds.main([str(self.repo.root), "--today", "2026-11-17", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_warn_mode_never_fails(self) -> None:
        self.repo.commit({"README.md": "[a](gone.md)\n"})
        code, out, _ = self.call()
        self.assertEqual(code, 0)
        self.assertIn("D101 WARNING README.md:1", out)
        self.assertIn("1 objective and 0 advisory", out)

    def test_enforce_fails_only_on_objective_findings(self) -> None:
        self.repo.commit({"README.md": "[a](gone.md)\n"})
        self.assertEqual(self.call("--mode", "enforce")[0], 1)
        self.repo.commit({"README.md": "fine\n"})
        self.assertEqual(self.call("--mode", "enforce")[0], 0)

    def test_github_event_annotations_and_summary(self) -> None:
        base = self.repo.commit({"README.md": "ok\n"})
        self.repo.commit({"README.md": "[a](gone.md)\n", "notes/x.md": "new\n"})
        event = self.repo.root.parent / f"{self.repo.root.name}-event.json"
        summary = self.repo.root.parent / f"{self.repo.root.name}-summary.md"
        self.addCleanup(event.unlink, missing_ok=True)
        self.addCleanup(summary.unlink, missing_ok=True)
        event.write_text(json.dumps({"pull_request": {"base": {"sha": base}, "body": None}}), encoding="utf-8")
        code, out, _ = self.call("--github", "--mode", "enforce",
                                 env={"GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(code, 1)
        self.assertIn("::error file=README.md,line=1,title=D101::link target does not exist: gone.md", out)
        self.assertIn("::warning file=notes/x.md,line=1,title=W204::", out)
        self.assertIn("| D101 | `README.md:1` |", summary.read_text(encoding="utf-8"))

    def test_github_without_pull_request_scans(self) -> None:
        self.repo.commit({"README.md": "ok\n"})
        event = self.repo.root.parent / f"{self.repo.root.name}-push.json"
        self.addCleanup(lambda: event.unlink(missing_ok=True))
        event.write_text(json.dumps({"ref": "refs/heads/main"}), encoding="utf-8")
        summary = self.repo.root.parent / f"{self.repo.root.name}-s.md"
        self.addCleanup(lambda: summary.unlink(missing_ok=True))
        code, _, _ = self.call("--github", env={"GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(code, 0)
        self.assertIn("No findings.", summary.read_text(encoding="utf-8"))
        self.assertEqual(ds.event_context(None), (None, None))

    def test_body_file_and_errors(self) -> None:
        rule = {"high_impact": [{"id": "x", "paths": ["x.txt"], "docs": ["docs/**"]}]}
        base = self.repo.commit({"x.txt": "a\n", ".github/docs-standard.json": json.dumps(rule)})
        self.repo.commit({"x.txt": "b\n"})
        body = self.repo.root.parent / f"{self.repo.root.name}-body.md"
        self.addCleanup(lambda: body.unlink(missing_ok=True))
        body.write_text("Documentation impact: none\n", encoding="utf-8")
        code, out, _ = self.call("--base", base, "--body-file", str(body))
        self.assertIn("needs a reason", out)
        self.repo.write({".github/docs-standard.json": "{\"nope\": 1}"})
        self.assertEqual(self.call()[0], 2)
        self.repo.write({".github/docs-standard.json": "{}"})
        code, _, err = self.call("--base", "0" * 40)
        self.assertEqual(code, 2)
        self.assertIn("git failed", err)

    def test_script_entry_point(self) -> None:
        self.repo.commit({"README.md": "[a](gone.md)\n"})
        script = Path(ds.__file__).resolve()
        result = subprocess.run([sys.executable, "-I", str(script), str(self.repo.root), "--mode", "enforce"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("D101 ERROR README.md:1", result.stdout)

    def test_escape(self) -> None:
        self.assertEqual(ds.escape("a%\r\nb"), "a%25%0D%0Ab")


if __name__ == "__main__":
    unittest.main()
