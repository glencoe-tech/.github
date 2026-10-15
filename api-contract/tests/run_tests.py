#!/usr/bin/env python3
"""Self-test for the api-contract checks.

Each case builds a tiny git history at test time (an `origin` bare repository, a clone, commits,
release tags, a pull-request branch) from the synthetic fixtures in ../fixtures, runs one check
exactly as the action does (src/contract_check.py, same environment variables, results read back
from $GITHUB_OUTPUT), and asserts the RESULT and the REASON code, not only the exit status. Every
failure case has a positive control: the same history with only the defect removed passes.

    python3 api-contract/tests/run_tests.py [--toolchain DIR] [-k SUBSTRING] [--keep]
    python3 api-contract/tests/run_tests.py --build CASE --into DIR   # history only (workflow use)

Needs the pinned toolchain (bin/install-toolchain.sh); without --toolchain it installs into
$RUNNER_TEMP or a temporary directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
CHECKER = ROOT / "src" / "contract_check.py"
WIDGETS = (FIXTURES / "widgets.yaml").read_text()

TODAY = dt.datetime.now(dt.timezone.utc).date()
FUTURE = (TODAY + dt.timedelta(days=30)).isoformat()
PAST = (TODAY - dt.timedelta(days=1)).isoformat()
TOO_FAR = (TODAY + dt.timedelta(days=400)).isoformat()

REMOVAL_ID = "api-path-removed-without-deprecation GET /widgets/{widgetId}"

# --------------------------------------------------------------------------- documents


def doc(version: str = "1.0.0", text: str = WIDGETS) -> str:
    return text.replace("__VERSION__", version)


def edit(text: str, old: str, new: str) -> str:
    if old not in text:
        raise AssertionError(f"fixture edit target not found: {old!r}")
    return text.replace(old, new, 1)


NEW_PATH = textwrap.indent(textwrap.dedent("""\
    /widgets/{widgetId}/parts:
      get:
        operationId: listWidgetParts
        summary: List parts
        description: Lists the parts of one widget.
        tags: [widgets]
        security:
          - bearer: []
        parameters:
          - name: widgetId
            in: path
            required: true
            description: Widget identifier.
            schema:
              type: string
        responses:
          "200":
            description: A page of widgets.
            content:
              application/json:
                schema:
                  $ref: "#/components/schemas/WidgetPage"
          "404":
            description: Not found.
            content:
              application/json:
                schema:
                  $ref: "#/components/schemas/Error"
    """), "  ")


def additive(version: str) -> str:
    return edit(doc(version), "components:\n", NEW_PATH + "components:\n")


def removed(version: str) -> str:
    text = doc(version)
    start = text.index("  /widgets/{widgetId}:\n")
    end = text.index("components:\n")
    return text[:start] + text[end:]


def cosmetic(version: str) -> str:
    return edit(doc(version), "description: Lists widgets.", "description: Lists every widget, one page at a time.")


def public_list(version: str) -> str:
    return edit(doc(version), "      tags: [widgets]\n      security:\n        - bearer: []\n",
                "      tags: [widgets]\n      security: []\n")


POLICY = "version: 1\ncontract: openapi.yaml\n"
SDK_POLICY = POLICY + textwrap.dedent("""\
    sdk:
      install: python3 tools/install.py
      generate: python3 tools/generate.py
      output: sdk
      lockfile: sdk.lock
    """)


def with_exception(finding: str, expires: str, policy: str = POLICY) -> str:
    return policy + f"exceptions:\n  - id: \"{finding}\"\n    reason: synthetic test exception\n    expires: \"{expires}\"\n"


# --------------------------------------------------------------------------- git fixture


def sh(cmd: list[str], cwd: Path, check: bool = True) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed in {cwd}: {proc.stderr}")
    return proc.stdout


class Repo:
    """origin (bare) + a working clone. `base` is the main commit the PR is judged against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.work = root / "work"
        sh(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin)], root)
        sh(["git", "init", "--quiet", "--initial-branch=main", str(self.work)], root)
        sh(["git", "remote", "add", "origin", str(self.origin)], self.work)
        self.base = ""

    def git(self, *args: str) -> str:
        return sh(["git", *args], self.work)

    def write(self, files: dict[str, str | None]) -> None:
        for name, content in files.items():
            path = self.work / name
            if content is None:
                if path.exists():
                    self.git("rm", "-q", "-r", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def commit(self, files: dict[str, str | None], message: str) -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def tag(self, name: str, ref: str = "HEAD") -> None:
        self.git("tag", "-a", name, "-m", name, ref)

    def push(self) -> None:
        self.git("push", "-q", "origin", "--all")
        self.git("push", "-q", "origin", "--tags")

    def release(self, files: dict[str, str | None], tag: str | None = "contract-v1.0.0") -> None:
        """main: the released state (optionally tagged), pushed. Then a `pr` branch from it."""
        self.base = self.commit(files, "main")
        if tag:
            self.tag(tag)
        self.push()
        self.git("checkout", "-q", "-b", "pr")


@dataclass
class Case:
    name: str
    check: str
    build: Callable[[Repo], Path | None]
    result: str
    reason: str
    outputs: dict[str, str] = field(default_factory=dict)
    first_release: str = "false"


def base_files(policy: str = POLICY, version: str = "1.0.0", text: str | None = None) -> dict[str, str | None]:
    return {".github/api-contract.yml": policy, "openapi.yaml": text if text is not None else doc(version)}


def pr(files_main: dict[str, str | None], files_pr: dict[str, str | None], tag: str | None = "contract-v1.0.0",
       after: Callable[[Repo], Path | None] | None = None) -> Callable[[Repo], Path | None]:
    def build(repo: Repo) -> Path | None:
        repo.release(files_main, tag)
        repo.commit(files_pr, "pr change")
        repo.push()
        return after(repo) if after else None
    return build


def shallow(repo: Repo) -> Path:
    target = repo.root / "shallow"
    sh(["git", "clone", "-q", "--depth", "2", "--branch", "pr", f"file://{repo.origin}", str(target)], repo.root)
    return target


def tag_off_history(repo: Repo) -> None:
    side = repo.git("rev-parse", "HEAD").strip()
    repo.git("checkout", "-q", "-b", "side", repo.base)
    repo.commit({"openapi.yaml": doc("1.1.0")}, "unmerged release")
    repo.tag("contract-v1.1.0")
    repo.push()
    repo.git("checkout", "-q", "pr")
    assert repo.git("rev-parse", "HEAD").strip() == side


def tag_only_on_origin(repo: Repo) -> None:
    other = repo.root / "other"
    sh(["git", "clone", "-q", str(repo.origin), str(other)], repo.root)
    sh(["git", "tag", "-a", "contract-v1.0.1", "-m", "x", "main"], other)
    sh(["git", "push", "-q", "origin", "contract-v1.0.1"], other)


def two_releases(repo: Repo) -> None:
    """contract-v1.9.0, then contract-v1.10.0: the latest is 1.10.0 by precedence, not by text."""
    repo.commit(base_files(version="1.9.0"), "release 1.9.0")
    repo.tag("contract-v1.9.0")
    repo.release({"openapi.yaml": doc("1.10.0")}, "contract-v1.10.0")
    repo.commit({"openapi.yaml": additive("1.11.0")}, "pr change")
    repo.push()


def sdk_files(operations: str, lock: str | None = None) -> dict[str, str | None]:
    files = {
        "tools/generate.py": (FIXTURES / "tools" / "generate.py").read_text(),
        "tools/install.py": (FIXTURES / "tools" / "install.py").read_text(),
        "sdk/operations.txt": operations,
    }
    if lock is not None:
        files["sdk.lock"] = lock
    return files


def lock_for_generator() -> str:
    import hashlib
    digest = hashlib.sha256((FIXTURES / "tools" / "generate.py").read_bytes()).hexdigest()
    return f"tools/generate.py sha256:{digest}\n"


GENERATED = "getWidget\nlistWidgets\n"
EXT_RULESET = textwrap.dedent("""\
    rules:
      product-info-owner:
        description: The contract names its owning team.
        severity: error
        given: "$.info"
        then:
          field: x-owner
          function: truthy
    """)
WEAKENING_RULESET = textwrap.dedent("""\
    extends: [[spectral:oas, off]]
    rules:
      gacs-operation-security-declared: off
      operation-success-response: off
    """)
NO_OP_SECURITY = edit(doc(), "      tags: [widgets]\n      security:\n        - bearer: []\n", "      tags: [widgets]\n")
OASDIFF_INJECTION = "unmatch-path: \".*\"\nexclude-elements: endpoints\n"

CASES: list[Case] = [
    # ---- validate (GACS-1 §5.A.1, §5.A.7)
    Case("validate/valid-3.1 (positive control)", "validate", pr(base_files(), {}), "pass", "ok"),
    Case("validate/3.0-construct-under-3.1-header", "validate",
         pr(base_files(), {"openapi.yaml": edit(doc(), 'type: [string, "null"]', "type: string\n          nullable: true")}),
         "fail", "openapi-3.0-construct"),
    Case("validate/schema-invalid (no info.title)", "validate",
         pr(base_files(), {"openapi.yaml": edit(doc(), "  title: Widgets (synthetic test fixture)\n", "")}),
         "fail", "schema-invalid"),
    Case("validate/openapi-3.0-header", "validate",
         pr(base_files(), {"openapi.yaml": edit(doc(), "openapi: 3.1.0", "openapi: 3.0.3")}), "fail", "not-openapi-3.1"),
    Case("validate/version-not-semver", "validate", pr(base_files(), {"openapi.yaml": doc("1.0")}),
         "fail", "invalid-contract-version"),
    Case("validate/unparseable (duplicate key)", "validate",
         pr(base_files(), {"openapi.yaml": edit(doc(), "openapi: 3.1.0\n", "openapi: 3.1.0\nopenapi: 3.1.1\n")}),
         "fail", "parse-error"),
    # ---- lint (GACS-1 §5.A.2-6)
    Case("lint/baseline-clean (positive control)", "lint", pr(base_files(), {}), "pass", "ok"),
    Case("lint/operation-without-security", "lint", pr(base_files(), {"openapi.yaml": NO_OP_SECURITY}),
         "fail", "lint-baseline"),
    Case("lint/product-extension-from-base", "lint",
         pr(base_files(POLICY + "lint:\n  ruleset: .github/contract.spectral.yaml\n") |
            {".github/contract.spectral.yaml": EXT_RULESET}, {}),
         "fail", "lint-extension"),
    Case("lint/product-extension-satisfied (positive control)", "lint",
         pr(base_files(POLICY + "lint:\n  ruleset: .github/contract.spectral.yaml\n") |
            {".github/contract.spectral.yaml": EXT_RULESET},
            {"openapi.yaml": edit(doc(), "  version: 1.0.0\n", "  version: 1.0.0\n  x-owner: widgets-team\n")}),
         "pass", "ok"),
    Case("lint/extension-cannot-weaken-baseline", "lint",
         pr(base_files(POLICY + "lint:\n  ruleset: .github/contract.spectral.yaml\n") |
            {".github/contract.spectral.yaml": WEAKENING_RULESET}, {"openapi.yaml": NO_OP_SECURITY}),
         "fail", "lint-baseline"),
    # ---- compatibility (GACS-1 §7, §8, Appendix A)
    Case("compat/additive+minor (positive control)", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.1.0")}), "pass", "ok",
         {"required-step": "minor", "declared-step": "minor", "baseline": "contract-v1.0.0"}),
    Case("compat/additive+no-bump", "compatibility", pr(base_files(), {"openapi.yaml": additive("1.0.0")}),
         "fail", "version-step-insufficient", {"required-step": "minor", "declared-step": "none"}),
    Case("compat/additive+patch", "compatibility", pr(base_files(), {"openapi.yaml": additive("1.0.1")}),
         "fail", "version-step-insufficient", {"required-step": "minor", "declared-step": "patch"}),
    Case("compat/endpoint-removed+minor", "compatibility", pr(base_files(), {"openapi.yaml": removed("1.1.0")}),
         "fail", "version-step-insufficient", {"required-step": "major"}),
    Case("compat/endpoint-removed+major (positive control)", "compatibility",
         pr(base_files(), {"openapi.yaml": removed("2.0.0")}), "pass", "ok", {"required-step": "major"}),
    Case("compat/auth-added-to-public-route+minor (severity override)", "compatibility",
         pr(base_files(text=public_list("1.0.0")), {"openapi.yaml": doc("1.1.0")}),
         "fail", "version-step-insufficient", {"required-step": "major"}),
    Case("compat/cosmetic+no-bump", "compatibility", pr(base_files(), {"openapi.yaml": cosmetic("1.0.0")}),
         "fail", "version-step-insufficient", {"required-step": "patch"}),
    Case("compat/cosmetic+patch (positive control)", "compatibility",
         pr(base_files(), {"openapi.yaml": cosmetic("1.0.1")}), "pass", "ok", {"required-step": "patch"}),
    Case("compat/unchanged (compared, not trusted by bytes)", "compatibility", pr(base_files(), {}), "pass", "ok",
         {"required-step": "none", "declared-step": "none"}),
    Case("compat/version-regressed", "compatibility", pr(base_files(), {"openapi.yaml": doc("0.9.0")}),
         "fail", "version-regressed"),
    Case("compat/no-tags-no-bootstrap", "compatibility", pr(base_files(), {}, tag=None),
         "fail", "no-released-baseline"),
    Case("compat/no-tags-with-bootstrap (positive control)", "compatibility", pr(base_files(), {}, tag=None),
         "pass", "ok", {"baseline": "none"}, first_release="true"),
    Case("compat/bootstrap-with-releases", "compatibility", pr(base_files(), {}), "fail",
         "first-release-with-releases", first_release="true"),
    Case("compat/shallow-clone", "compatibility", pr(base_files(), {"openapi.yaml": additive("1.1.0")}, after=shallow),
         "fail", "shallow-clone"),
    Case("compat/malformed-tag", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.1.0")}, after=lambda r: (r.tag("contract-vlatest"), r.push(), None)[2]),
         "fail", "malformed-tag"),
    Case("compat/prerelease-latest-tag", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.1.0")},
            after=lambda r: (r.tag("contract-v1.1.0-rc.1"), r.push(), None)[2]),
         "fail", "prerelease-baseline"),
    Case("compat/older-prerelease-ignored (positive control)", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.1.0")},
            after=lambda r: (r.tag("contract-v1.0.0-rc.1", r.base), r.push(), None)[2]),
         "pass", "ok", {"baseline": "contract-v1.0.0"}),
    Case("compat/semver-precedence-1.10>1.9 (positive control)", "compatibility",
         two_releases,
         "pass", "ok", {"baseline": "contract-v1.10.0"}),
    Case("compat/tag-not-in-history", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.2.0")}, after=tag_off_history), "fail", "tag-not-in-history"),
    Case("compat/local-tags-differ-from-origin", "compatibility",
         pr(base_files(), {"openapi.yaml": additive("1.1.0")}, after=tag_only_on_origin), "fail", "tags-differ-from-remote"),
    Case("compat/tag-does-not-release-what-it-names", "compatibility",
         pr(base_files(version="0.9.0"), {"openapi.yaml": additive("1.1.0")}), "fail", "tag-version-mismatch"),
    Case("compat/head-loosens-exceptions (base policy used)", "compatibility",
         pr(base_files(), {"openapi.yaml": removed("1.1.0"), ".github/api-contract.yml": with_exception(REMOVAL_ID, FUTURE)}),
         "fail", "version-step-insufficient", {"required-step": "major"}),
    Case("compat/exception-granted-at-base (positive control)", "compatibility",
         pr(base_files(with_exception(REMOVAL_ID, FUTURE)), {"openapi.yaml": removed("1.1.0")}),
         "pass", "ok", {"required-step": "patch"}),
    Case("compat/expired-exception", "compatibility",
         pr(base_files(with_exception(REMOVAL_ID, PAST)), {"openapi.yaml": removed("2.0.0")}),
         "fail", "exception-expired"),
    Case("compat/exception-without-reason", "compatibility",
         pr(base_files(POLICY + f"exceptions:\n  - id: \"{REMOVAL_ID}\"\n    expires: \"{FUTURE}\"\n"),
            {"openapi.yaml": removed("2.0.0")}),
         "fail", "exception-invalid"),
    Case("compat/exception-beyond-180-days", "compatibility",
         pr(base_files(with_exception(REMOVAL_ID, TOO_FAR)), {"openapi.yaml": removed("2.0.0")}),
         "fail", "exception-invalid"),
    Case("compat/oasdiff-config-injected-by-head", "compatibility",
         pr(base_files(), {"openapi.yaml": removed("1.1.0"), ".oasdiff.yaml": OASDIFF_INJECTION}),
         "fail", "version-step-insufficient", {"required-step": "major"}),
    Case("compat/policy-missing-at-base", "compatibility",
         pr({"openapi.yaml": doc()}, {".github/api-contract.yml": POLICY, "openapi.yaml": additive("1.1.0")}),
         "fail", "policy-missing-at-base"),
    Case("compat/policy-unknown-key", "compatibility",
         pr(base_files(POLICY + "exception:\n  - id: x\n"), {"openapi.yaml": additive("1.1.0")}),
         "fail", "policy-invalid"),
    # ---- generated SDK (GACS-1 §6.3) and generator lockfile (§6.4)
    Case("sdk/reproducible (positive control)", "sdk",
         pr(base_files(SDK_POLICY) | sdk_files(GENERATED, lock_for_generator()), {}), "pass", "ok"),
    Case("sdk/hand-edited", "sdk",
         pr(base_files(SDK_POLICY) | sdk_files(GENERATED, lock_for_generator()),
            {"sdk/operations.txt": GENERATED + "deleteWidget\n"}), "fail", "sdk-drift"),
    Case("sdk/contract-changed-sdk-not-regenerated", "sdk",
         pr(base_files(SDK_POLICY) | sdk_files(GENERATED, lock_for_generator()), {"openapi.yaml": additive("1.1.0")}),
         "fail", "sdk-drift"),
    Case("sdk/not-configured", "sdk", pr(base_files(), {}), "fail", "sdk-not-configured"),
    Case("lockfile/pinned (positive control)", "lockfile",
         pr(base_files(SDK_POLICY) | sdk_files(GENERATED, lock_for_generator()), {}), "pass", "ok"),
    Case("lockfile/stale", "lockfile",
         pr(base_files(SDK_POLICY) | sdk_files(GENERATED, "tools/generate.py sha256:0000\n"), {}),
         "fail", "lockfile-drift"),
    Case("lockfile/not-committed", "lockfile", pr(base_files(SDK_POLICY) | sdk_files(GENERATED), {}),
         "fail", "lockfile-missing"),
]


# --------------------------------------------------------------------------- runner


def install_toolchain(target: Path) -> Path:
    subprocess.run([str(ROOT / "bin" / "install-toolchain.sh"), str(target)], check=True,
                   stdout=subprocess.DEVNULL)
    return target


def run_case(case: Case, scratch: Path, toolchain: Path) -> tuple[bool, str]:
    repo = Repo(scratch)
    workdir = case.build(repo) or repo.work
    out_file = scratch / "github_output"
    out_file.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "API_CONTRACT_"))}
    env.update(API_CONTRACT_TOOLCHAIN=str(toolchain), API_CONTRACT_BASE_SHA=repo.base,
               API_CONTRACT_FIRST_RELEASE=case.first_release, GITHUB_OUTPUT=str(out_file),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    proc = subprocess.run([sys.executable, str(CHECKER), "--check", case.check], cwd=workdir, env=env,
                          capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line)
    reasons = outputs.get("reasons", "").split()
    problems = []
    if outputs.get("result") != case.result:
        problems.append(f"result {outputs.get('result')!r}, expected {case.result!r}")
    if (proc.returncode == 0) != (case.result == "pass"):
        problems.append(f"exit code {proc.returncode} disagrees with expected {case.result}")
    if case.reason not in reasons:
        problems.append(f"reasons {reasons}, expected {case.reason!r}")
    if case.result == "pass" and reasons != ["ok"]:
        problems.append(f"a pass must carry no failure reason, got {reasons}")
    for key, value in case.outputs.items():
        if outputs.get(key) != value:
            problems.append(f"output {key}={outputs.get(key)!r}, expected {value!r}")
    detail = "; ".join(problems)
    if problems:
        detail += "\n" + textwrap.indent(proc.stdout + proc.stderr, "      | ")
    return not problems, detail


def unit_tests(toolchain: Path) -> list[str]:
    """Checks on the shipped configuration itself (no git history needed)."""
    sys.path.insert(0, str(ROOT / "src"))
    import contract_check as cc  # noqa: PLC0415

    failures = []
    order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
             "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.9.0", "1.10.0", "2.0.0"]
    if sorted(order, key=cc.semver_key) != order:
        failures.append("semver precedence (SemVer 2.0.0 §11) is wrong")
    for bad in ("01.0.0", "1.0", "1.0.0+build", "v1.0.0", "1.0.0-", "١.٠.٠"):
        if cc.SEMVER.fullmatch(bad):
            failures.append(f"{bad!r} accepted as a version")
    # Every id the severity file raises must be a check this oasdiff build knows; a typo would
    # silently leave that change at its default level.
    known = subprocess.run([str(toolchain / "bin" / "oasdiff"), "checks", "--format", "json"],
                           capture_output=True, text=True, check=True).stdout
    known_ids = {c["id"] for c in json.loads(known)}
    for line in (ROOT / "oasdiff" / "severity-levels.txt").read_text().splitlines():
        rule, level = line.split()
        if rule not in known_ids or level not in ("err", "warn", "info", "none"):
            failures.append(f"severity-levels.txt line {line!r} is not a known oasdiff check/level")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("-k", dest="match", default="")
    parser.add_argument("--keep", action="store_true", help="keep the scratch histories")
    parser.add_argument("--build", help="only build the named case's history")
    parser.add_argument("--into", type=Path, help="with --build: directory to build in")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for case in CASES:
            print(f"{case.name}\t{case.check}\t{case.result}\t{case.reason}")
        return 0
    if args.build:
        case = next((c for c in CASES if c.name == args.build), None)
        if case is None or args.into is None:
            parser.error("--build needs an exact case name (see --list) and --into")
        args.into.mkdir(parents=True, exist_ok=True)
        repo = Repo(args.into.resolve())
        workdir = case.build(repo) or repo.work
        print(json.dumps({"workdir": str(workdir), "base_sha": repo.base, "check": case.check,
                          "first_release": case.first_release, "result": case.result, "reason": case.reason}))
        return 0

    scratch_root = Path(tempfile.mkdtemp(prefix="api-contract-test-", dir=os.environ.get("RUNNER_TEMP")))
    toolchain = args.toolchain.resolve() if args.toolchain else install_toolchain(scratch_root / "toolchain")
    failed = 0
    for problem in unit_tests(toolchain):
        print(f"FAIL unit: {problem}")
        failed += 1
    selected = [c for c in CASES if args.match in c.name]
    for i, case in enumerate(selected):
        scratch = scratch_root / f"case-{i:02d}"
        scratch.mkdir()
        try:
            ok, detail = run_case(case, scratch, toolchain)
        except Exception as exc:  # noqa: BLE001 - a broken fixture is a test failure
            ok, detail = False, f"fixture error: {exc}"
        print(f"{'PASS' if ok else 'FAIL'} {case.name}: expect {case.result.upper()} ({case.reason})"
              + ("" if ok else f"\n    {detail}"))
        failed += 0 if ok else 1
    print(f"\n{len(selected) - failed if failed <= len(selected) else 0}/{len(selected)} cases as expected; "
          f"{failed} failure(s)")
    if not args.keep:
        shutil.rmtree(scratch_root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
