#!/usr/bin/env python3
"""The api-contract checks (GACS-1 §5.A, §7, §8, §14, §17), one per invocation.

    contract_check.py --check validate|lint|compatibility|sdk|lockfile

Runs in the root of the repository under test (a full clone, tags fetched). Reads:

  API_CONTRACT_TOOLCHAIN  directory written by bin/install-toolchain.sh (oasdiff, Spectral, yaml)
  API_CONTRACT_BASE_SHA   the revision the POLICY is read from: the pull request's base
  API_CONTRACT_POLICY     repository path of the policy file (default .github/api-contract.yml)
  API_CONTRACT_FIRST_RELEASE  "true" only for the reviewed first release of a contract
  GITHUB_OUTPUT, GITHUB_STEP_SUMMARY  when set (in Actions), results are written there too

Every refusal carries a stable reason code (README "Reason codes"). Exit 0 = pass, 1 = fail.

Python 3.11+, standard library only. The YAML parser, oasdiff and Spectral come from the pinned
toolchain. The policy is read from the BASE revision so a pull request cannot loosen the gate it
is judged by; the contract, the SDK and the lockfile are read from the revision under test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ACTION_ROOT = HERE.parent
VALIDATE_RULESET = ACTION_ROOT / "rulesets" / "validate.spectral.yaml"
BASELINE_RULESET = ACTION_ROOT / "rulesets" / "baseline.spectral.yaml"
SEVERITY_LEVELS = ACTION_ROOT / "oasdiff" / "severity-levels.txt"
TO_JSON = HERE / "to_json.mjs"

CHECKS = ("validate", "lint", "compatibility", "sdk", "lockfile")
DEFAULT_POLICY = ".github/api-contract.yml"
DEFAULT_TAG_PREFIX = "contract-v"
EXCEPTION_MAX_DAYS = 180  # GACS-1 standard.json windows.exception_max_days
STEPS = ("none", "patch", "minor", "major")
JSON_SCHEMA_2020_12 = {
    "https://spec.openapis.org/oas/3.1/dialect/base",
    "https://json-schema.org/draft/2020-12/schema",
}

# Semantic Versioning 2.0.0, ASCII digits only, no leading zeros. Build metadata ("+...") is not
# accepted on a release tag: two tags differing only in it would have equal precedence.
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(rf"^({_NUM})\.({_NUM})\.({_NUM})(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?$")
RELEASE = re.compile(rf"^({_NUM})\.({_NUM})\.({_NUM})$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TAG_PREFIX_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Keys whose children are NAMES chosen by the author, not OpenAPI/JSON Schema keywords. Inside
# these maps a key called "description" is a property, a schema or a response named so.
NAME_MAPS = {
    "properties", "patternProperties", "dependentSchemas", "$defs", "definitions", "schemas",
    "responses", "parameters", "examples", "headers", "requestBodies", "securitySchemes", "links",
    "callbacks", "pathItems", "paths", "webhooks", "content", "encoding", "variables", "mapping",
    "scopes",
}
COSMETIC_KEYS = {"description", "summary", "example", "examples", "externalDocs"}


class Refusal(Exception):
    """A check outcome that is a failure with a reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Outcome:
    check: str
    failures: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    passes: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)

    def fail(self, code: str, message: str) -> None:
        self.failures.append((code, message))

    @property
    def ok(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------- output


def _escape(text: str) -> str:
    """Escape data for a workflow command, so a crafted string cannot start a new command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def log(text: str = "") -> None:
    # Every plain line is indented, so no line of data can begin with "::" and become a command.
    for line in text.splitlines() or [""]:
        print(f"  {line}" if line else "")


def annotate(kind: str, title: str, message: str) -> None:
    print(f"::{kind} title={_escape(title).replace(',', '%2C').replace(':', '%3A')}::{_escape(message)}")


# --------------------------------------------------------------------------- process helpers


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = False, text_input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, input=text_input, capture_output=True,
                          text=True, check=check)


def git(*args: str) -> str:
    try:
        return run(["git", *args], check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise Refusal("git-error", f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    except OSError as exc:
        raise Refusal("git-error", f"git could not be run here: {exc}") from exc


def git_ok(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args])


def git_bytes(*args: str) -> bytes | None:
    proc = subprocess.run(["git", *args], capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def tool_env() -> dict[str, str]:
    """The environment for oasdiff and Spectral: nothing that can reconfigure them.

    oasdiff reads OASDIFF_* variables and a .oasdiff.* file from its working directory; either
    could exclude paths from the comparison. Tools run with those removed, from an empty
    directory, on absolute paths.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("OASDIFF")}
    env["NO_COLOR"] = "1"
    return env


def toolchain() -> Path:
    raw = os.environ.get("API_CONTRACT_TOOLCHAIN", "")
    if not raw:
        raise Refusal("tool-error", "API_CONTRACT_TOOLCHAIN is not set (run bin/install-toolchain.sh)")
    path = Path(raw).resolve()
    for need in (path / "bin" / "oasdiff", path / "node_modules" / ".bin" / "spectral"):
        if not need.exists():
            raise Refusal("tool-error", f"pinned toolchain incomplete: {need} is missing")
    return path


# --------------------------------------------------------------------------- documents


def parse_document(path: Path, label: str) -> object:
    env = dict(os.environ, API_CONTRACT_TOOLCHAIN=str(toolchain()))
    proc = run(["node", str(TO_JSON), str(path)], env=env)
    if proc.returncode != 0:
        raise Refusal("parse-error", f"{label} does not parse: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def parse_bytes(data: bytes, suffix: str, label: str, workdir: Path) -> object:
    target = workdir / f"doc-{len(list(workdir.glob('doc-*')))}{suffix}"
    target.write_bytes(data)
    return parse_document(target, label)


def repo_path(raw: object, what: str) -> PurePosixPath:
    """A relative path inside the repository, or a policy refusal."""
    if not isinstance(raw, str) or not raw.strip():
        raise Refusal("policy-invalid", f"{what} must be a non-empty string")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or str(path) in (".", ""):
        raise Refusal("policy-invalid", f"{what} '{raw}' must be a path inside the repository")
    return path


# --------------------------------------------------------------------------- policy (from BASE)


@dataclass
class Policy:
    contract: PurePosixPath
    tag_prefix: str
    lint_ruleset: PurePosixPath | None
    exceptions: list[dict[str, str]]
    sdk: dict[str, str] | None
    base_sha: str
    path: str


def _require_keys(obj: object, allowed: set[str], required: set[str], where: str) -> dict:
    if not isinstance(obj, dict):
        raise Refusal("policy-invalid", f"{where} must be a mapping")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise Refusal("policy-invalid", f"{where} has unknown keys {unknown} (a typo would silently disable a rule)")
    missing = sorted(required - set(obj))
    if missing:
        raise Refusal("policy-invalid", f"{where} is missing {missing}")
    return obj


def load_policy(workdir: Path) -> Policy:
    base = os.environ.get("API_CONTRACT_BASE_SHA", "").strip()
    policy_path = os.environ.get("API_CONTRACT_POLICY", "").strip() or DEFAULT_POLICY
    repo_path(policy_path, "policy path")
    if git("rev-parse", "--show-prefix").strip():
        raise Refusal("input-invalid", "run from the repository root (working-directory must be the clone's root)")
    if not base:
        raise Refusal("base-unknown", "no base revision given: the policy is read from the base, never the head")
    if git_ok("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise Refusal("base-unknown", f"base revision {base} is not in this clone (fetch-depth: 0 is required)")
    base = git("rev-parse", f"{base}^{{commit}}").strip()
    raw = git_bytes("show", f"{base}:{policy_path}")
    if raw is None:
        raise Refusal(
            "policy-missing-at-base",
            f"{policy_path} does not exist at the base revision {base[:12]}. The policy is read from "
            "the base so a pull request cannot loosen it: land the policy file first, then the check.",
        )
    data = parse_bytes(raw, Path(policy_path).suffix or ".yml", f"policy {policy_path}@base", workdir)
    data = _require_keys(data, {"version", "contract", "tag-prefix", "lint", "exceptions", "sdk"},
                         {"version", "contract"}, f"policy {policy_path}")
    if data["version"] != 1:
        raise Refusal("policy-invalid", f"policy version must be 1, found {data['version']!r}")
    prefix = data.get("tag-prefix", DEFAULT_TAG_PREFIX)
    if not isinstance(prefix, str) or not TAG_PREFIX_OK.fullmatch(prefix):
        raise Refusal("policy-invalid", f"tag-prefix {prefix!r} must be a plain tag name prefix")
    lint_ruleset = None
    if "lint" in data:
        lint = _require_keys(data["lint"], {"ruleset"}, {"ruleset"}, "policy lint")
        lint_ruleset = repo_path(lint["ruleset"], "lint.ruleset")
    exceptions = data.get("exceptions") or []
    if not isinstance(exceptions, list):
        raise Refusal("policy-invalid", "exceptions must be a list")
    sdk = None
    if "sdk" in data:
        sdk = _require_keys(data["sdk"], {"generate", "output", "lockfile", "install"},
                            {"generate", "output", "lockfile"}, "policy sdk")
        for key in ("generate", "install"):
            if key in sdk and (not isinstance(sdk[key], str) or not sdk[key].strip()):
                raise Refusal("policy-invalid", f"sdk.{key} must be a non-empty command")
        repo_path(sdk["output"], "sdk.output")
        repo_path(sdk["lockfile"], "sdk.lockfile")
    head_policy = Path(policy_path)
    if not head_policy.is_file() or head_policy.read_bytes() != raw:
        annotate("notice", "api-contract policy",
                 f"{policy_path} differs between the base and this revision. The BASE policy is in "
                 "effect; the change applies once it is merged (review it as a contract change).")
    return Policy(repo_path(data["contract"], "contract"), prefix, lint_ruleset, exceptions, sdk, base, policy_path)


# --------------------------------------------------------------------------- semver


def semver_key(version: str) -> tuple:
    m = SEMVER.fullmatch(version)
    if not m:
        raise ValueError(version)
    core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    if pre is None:
        return (*core, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (*core, 0, ids)


def release_tuple(version: object) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    m = RELEASE.fullmatch(version)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def step_between(old: tuple[int, int, int], new: tuple[int, int, int]) -> str:
    if new[0] != old[0]:
        return "major"
    if new[1] != old[1]:
        return "minor"
    if new[2] != old[2]:
        return "patch"
    return "none"


# --------------------------------------------------------------------------- validate


def walk_30_constructs(node: object, path: str, found: list[str]) -> None:
    """OpenAPI 3.0 schema constructs under a 3.1 header (GACS-1 §5.A.1).

    In 3.1 `nullable` does not exist and `exclusiveMinimum`/`exclusiveMaximum` are numbers. A
    boolean under one of those keys is a 3.0 construct; a property *named* so has a mapping value
    and is not reported.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in ("nullable", "exclusiveMinimum", "exclusiveMaximum") and isinstance(value, bool):
                found.append(f"{here}: {key}: {str(value).lower()} is OpenAPI 3.0 syntax")
            walk_30_constructs(value, here, found)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk_30_constructs(item, f"{path}/{i}", found)


def run_spectral(ruleset: Path, document: Path, workdir: Path) -> tuple[list[dict], list[dict]]:
    """(errors, warnings) of a Spectral run. A run that did not complete is a refusal."""
    out = workdir / f"spectral-{ruleset.stem}.json"
    out.unlink(missing_ok=True)
    spectral = toolchain() / "node_modules" / ".bin" / "spectral"
    proc = run([str(spectral), "lint", str(document), "--ruleset", str(ruleset), "--format", "json",
                "--output", str(out), "--fail-severity", "error", "--quiet"],
               cwd=workdir, env=tool_env())
    if proc.returncode not in (0, 1) or not out.is_file():
        raise Refusal("tool-error", f"Spectral did not complete (exit {proc.returncode}): "
                      f"{(proc.stderr or proc.stdout).strip()[:2000]}")
    results = json.loads(out.read_text() or "[]")
    errors = [r for r in results if r.get("severity") == 0]
    warnings = [r for r in results if r.get("severity") == 1]
    if proc.returncode == 1 and not errors:
        raise Refusal("tool-error", "Spectral failed and reported no error-severity result")
    return errors, warnings


def describe(result: dict) -> str:
    where = "/".join(str(p) for p in result.get("path", []))
    line = (result.get("range", {}).get("start", {}).get("line", -1)) + 1
    return f"[{result.get('code')}] {where or '(document)'} (line {line}): {result.get('message')}"


def check_validate(policy: Policy, outcome: Outcome, workdir: Path) -> None:
    document = Path(str(policy.contract))
    if not document.is_file():
        raise Refusal("contract-missing", f"the contract {policy.contract} does not exist in this revision")
    doc = parse_document(document, str(policy.contract))
    if not isinstance(doc, dict):
        raise Refusal("not-openapi-3.1", "the contract is not a mapping")
    header = doc.get("openapi")
    if not isinstance(header, str) or not re.fullmatch(r"3\.1\.[0-9]+", header):
        outcome.fail("not-openapi-3.1", f"`openapi` is {header!r}; GACS-1 §5.A.1 requires 3.1.x")
    dialect = doc.get("jsonSchemaDialect")
    if dialect is not None and dialect not in JSON_SCHEMA_2020_12:
        outcome.fail("schema-dialect", f"jsonSchemaDialect {dialect!r} is not JSON Schema 2020-12")
    found: list[str] = []
    walk_30_constructs(doc, "#", found)
    for item in found:
        outcome.fail("openapi-3.0-construct", item)
    version = (doc.get("info") or {}).get("version") if isinstance(doc.get("info"), dict) else None
    if release_tuple(version) is None:
        outcome.fail("invalid-contract-version",
                     f"info.version is {version!r}; the contract version is MAJOR.MINOR.PATCH (GACS-1 §5.A.7, §7.1)")
    errors, _ = run_spectral(VALIDATE_RULESET, document.resolve(), workdir)
    for result in errors:
        outcome.fail("schema-invalid", describe(result))
    if outcome.ok:
        outcome.passes.append(f"{policy.contract} is a valid OpenAPI {header} document, version {version}")


# --------------------------------------------------------------------------- lint


def check_lint(policy: Policy, outcome: Outcome, workdir: Path) -> None:
    document = Path(str(policy.contract))
    if not document.is_file():
        raise Refusal("contract-missing", f"the contract {policy.contract} does not exist in this revision")
    runs = [("lint-baseline", "baseline ruleset (shipped with the action)", BASELINE_RULESET)]
    if policy.lint_ruleset is not None:
        raw = git_bytes("show", f"{policy.base_sha}:{policy.lint_ruleset}")
        if raw is None:
            raise Refusal("policy-invalid", f"lint.ruleset {policy.lint_ruleset} does not exist at the base revision")
        extension_dir = workdir / "extension"
        extension_dir.mkdir(exist_ok=True)
        extension = extension_dir / ("ruleset" + (Path(str(policy.lint_ruleset)).suffix or ".yaml"))
        extension.write_bytes(raw)
        runs.append(("lint-extension", f"product ruleset {policy.lint_ruleset}@base", extension))
    for code, label, ruleset in runs:
        errors, warnings = run_spectral(ruleset, document.resolve(), workdir)
        for result in errors:
            outcome.fail(code, describe(result))
        for result in warnings:
            annotate("warning", f"api-contract lint ({label})", describe(result))
        if not errors:
            outcome.passes.append(f"{label}: no errors ({len(warnings)} warnings)")


# --------------------------------------------------------------------------- compatibility


def local_tags(prefix: str) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(refname:strip=2) %(objectname)", f"refs/tags/{prefix}*")
    pairs = (line.split(" ", 1) for line in out.splitlines() if line.strip())
    return {name: oid for name, oid in pairs if name.startswith(prefix)}


def remote_tags(prefix: str) -> dict[str, str]:
    proc = git_ok("ls-remote", "--tags", "--refs", "origin")
    if proc.returncode != 0:
        raise Refusal("remote-unreadable",
                      f"git ls-remote origin failed, so the release tags cannot be confirmed complete: {proc.stderr.strip()}")
    tags = {}
    for line in proc.stdout.splitlines():
        oid, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        if ref.startswith("refs/tags/") and name.startswith(prefix):
            tags[name] = oid.strip()
    return tags


def strip_cosmetic(node: object, parent: str | None = None) -> object:
    """The document without what GACS-1 Appendix A calls cosmetic (description-or-example-change)."""
    if isinstance(node, dict):
        names = parent in NAME_MAPS
        return {k: strip_cosmetic(v, None if names else k) for k, v in node.items()
                if names or k not in COSMETIC_KEYS}
    if isinstance(node, list):
        return [strip_cosmetic(v, None) for v in node]
    return node


def cosmetic_view(doc: dict) -> object:
    """What remains when descriptions, examples and the info block are set aside."""
    copy = dict(doc)
    copy.pop("info", None)
    return strip_cosmetic(copy)


def without_version(doc: dict) -> dict:
    copy = json.loads(json.dumps(doc))
    if isinstance(copy.get("info"), dict):
        copy["info"].pop("version", None)
    return copy


def validate_exceptions(policy: Policy, outcome: Outcome) -> dict[str, dict[str, str]]:
    """GACS-1 §8.8 / §14: each entry names the finding, the reason and an expiry; expired fails."""
    today = dt.datetime.now(dt.timezone.utc).date()
    active: dict[str, dict[str, str]] = {}
    for i, entry in enumerate(policy.exceptions):
        where = f"exceptions[{i}]"
        if not isinstance(entry, dict) or set(entry) - {"id", "reason", "expires", "owner"}:
            outcome.fail("exception-invalid", f"{where} must be a mapping of id, reason, expires (and optional owner)")
            continue
        finding = entry.get("id")
        reason = entry.get("reason")
        expires = entry.get("expires")
        if not isinstance(finding, str) or not finding.strip():
            outcome.fail("exception-invalid", f"{where} has no finding id")
            continue
        if not isinstance(reason, str) or not reason.strip():
            outcome.fail("exception-invalid", f"{where} ({finding}) has no reason")
            continue
        if not isinstance(expires, str) or not DATE.fullmatch(expires):
            outcome.fail("exception-invalid", f"{where} ({finding}) has no expiry date (YYYY-MM-DD)")
            continue
        try:
            expiry = dt.date.fromisoformat(expires)
        except ValueError:
            outcome.fail("exception-invalid", f"{where} ({finding}) expiry {expires} is not a date")
            continue
        if expiry < today:
            outcome.fail("exception-expired",
                         f"{where} ({finding}) expired on {expires}; an expired exception is a failure (GACS-1 §14.4)")
            continue
        if (expiry - today).days > EXCEPTION_MAX_DAYS:
            outcome.fail("exception-invalid",
                         f"{where} ({finding}) expires {expires}, more than {EXCEPTION_MAX_DAYS} days away (GACS-1 §14.2)")
            continue
        active[" ".join(finding.split())] = entry
    return active


def finding_id(entry: dict) -> str:
    parts = [str(entry.get("id", "")).strip() or "unknown", str(entry.get("operation", "")).strip(),
             str(entry.get("path", "")).strip()]
    return " ".join(p for p in parts if p)


def run_oasdiff(base: Path, head: Path, workdir: Path) -> list[dict]:
    empty = workdir / "oasdiff-cwd"
    empty.mkdir(exist_ok=True)
    proc = run([str(toolchain() / "bin" / "oasdiff"), "changelog", "--severity-levels", str(SEVERITY_LEVELS),
                "--format", "json", str(base), str(head)], cwd=empty, env=tool_env())
    if proc.returncode != 0:
        raise Refusal("tool-error", f"oasdiff changelog failed (exit {proc.returncode}): {proc.stderr.strip()[:2000]}")
    data = json.loads(proc.stdout or "[]")
    if not isinstance(data, list):
        raise Refusal("tool-error", "oasdiff changelog did not return a list")
    return data


def required_step_of(level: object) -> str:
    """oasdiff level -> the smallest version step (GACS-1 Appendix A, standard.json classes).

    3 ERR = breaking -> major; 2 WARN = risky and 1 INFO = additive -> minor. Anything else that
    oasdiff emits is unclassified, and an unclassified change is breaking (§8.3): fail closed.
    """
    if isinstance(level, bool) or not isinstance(level, int):
        return "major"
    return {3: "major", 2: "minor", 1: "minor"}.get(level, "major")


def check_compatibility(policy: Policy, outcome: Outcome, workdir: Path, first_release: bool) -> None:
    prefix = policy.tag_prefix
    if git("rev-parse", "--is-shallow-repository").strip() != "false":
        raise Refusal("shallow-clone",
                      "this is a shallow clone: release tags and history may be missing, and a missing "
                      "newest release reads exactly like an older one. Check out with fetch-depth: 0.")
    local = local_tags(prefix)
    remote = remote_tags(prefix)
    missing = sorted(set(remote) - set(local))
    extra = sorted(set(local) - set(remote))
    moved = sorted(t for t in set(local) & set(remote) if local[t] != remote[t])
    if missing or extra or moved:
        detail = "; ".join(f"{label}: {', '.join(names)}" for label, names in
                           (("missing here", missing), ("not on origin", extra), ("pointing elsewhere", moved)) if names)
        raise Refusal("tags-differ-from-remote",
                      f"the local {prefix}* tags differ from origin's ({detail}). Fetch tags (fetch-tags: true).")
    malformed = sorted(t for t in local if not SEMVER.fullmatch(t[len(prefix):]))
    if malformed:
        raise Refusal("malformed-tag",
                      f"tags matching '{prefix}*' that are not {prefix}MAJOR.MINOR.PATCH[-pre]: {', '.join(malformed)}. "
                      "Which release is the latest would be a guess.")
    head_doc_path = Path(str(policy.contract))
    if not head_doc_path.is_file():
        raise Refusal("contract-missing", f"the contract {policy.contract} does not exist in this revision")
    head_doc = parse_document(head_doc_path, str(policy.contract))
    declared_raw = head_doc.get("info", {}).get("version") if isinstance(head_doc, dict) and isinstance(head_doc.get("info"), dict) else None
    declared = release_tuple(declared_raw)
    if declared is None:
        raise Refusal("invalid-contract-version", f"info.version is {declared_raw!r}; it must be MAJOR.MINOR.PATCH")
    exceptions = validate_exceptions(policy, outcome)

    if not local:
        if not first_release:
            raise Refusal("no-released-baseline",
                          f"no {prefix}* release tag exists here or on origin, so there is no released contract to "
                          "measure this change from. A first release passes only with the input first-release: true.")
        outcome.outputs.update({"baseline": "none", "required-step": "none", "declared-step": "first-release"})
        outcome.passes.append(f"first release: no {prefix}* tag exists here or on origin; {declared_raw} is the first version")
        return
    if first_release:
        raise Refusal("first-release-with-releases",
                      f"first-release: true was given and releases exist ({', '.join(sorted(local))}): a first release cannot follow one")

    latest = max(local, key=lambda t: semver_key(t[len(prefix):]))
    latest_version = latest[len(prefix):]
    if not RELEASE.fullmatch(latest_version):
        raise Refusal("prerelease-baseline",
                      f"the highest tag {latest} is a pre-release, and a pre-release is never the last released "
                      "version (GACS-1 §7.6). Release it, or remove the tag.")
    if git_ok("merge-base", "--is-ancestor", latest, "HEAD").returncode != 0:
        raise Refusal("tag-not-in-history",
                      f"the latest release {latest} is not in the history of this revision: it would be compared "
                      "with a release it does not descend from. Update the branch.")
    released_bytes = git_bytes("show", f"{latest}:{policy.contract}")
    if released_bytes is None:
        raise Refusal("tag-missing-contract", f"the release {latest} carries no {policy.contract}")
    base_file = workdir / ("released" + (head_doc_path.suffix or ".yaml"))
    base_file.write_bytes(released_bytes)
    base_doc = parse_document(base_file, f"{policy.contract}@{latest}")
    released_raw = base_doc.get("info", {}).get("version") if isinstance(base_doc, dict) and isinstance(base_doc.get("info"), dict) else None
    if released_raw != latest_version:
        raise Refusal("tag-version-mismatch",
                      f"{latest} does not release what it names: its contract declares info.version {released_raw!r}")
    released = release_tuple(latest_version)
    assert released is not None
    outcome.outputs["baseline"] = latest
    if declared < released:
        raise Refusal("version-regressed", f"info.version {declared_raw} is lower than the last release {latest_version}")

    # Always compare. Equal bytes or digests are never taken as a compatibility pass on their own.
    entries = run_oasdiff(base_file.resolve(), head_doc_path.resolve(), workdir)
    required = "none"
    if without_version(base_doc) != without_version(head_doc):
        required = "patch"  # different bytes under an existing version are never allowed (§7.2)
    used: set[str] = set()
    counted: list[tuple[str, str, dict]] = []
    for entry in entries:
        fid = finding_id(entry)
        if fid in exceptions:
            used.add(fid)
            log(f"- excepted ({exceptions[fid]['reason']}, until {exceptions[fid]['expires']}): {fid}")
            continue
        step = required_step_of(entry.get("level"))
        counted.append((fid, step, entry))
    if not entries and cosmetic_view(base_doc) != cosmetic_view(head_doc):
        # The documents differ beyond descriptions and examples, and the tool classified nothing.
        fid = "unclassified-change"
        if fid in exceptions:
            used.add(fid)
        else:
            counted.append((fid, "major", {"text": "the contract changed in a way oasdiff does not classify; "
                                                   "an unclassified change is breaking until classified (GACS-1 §8.3)"}))
    for fid, step, entry in counted:
        if STEPS.index(step) > STEPS.index(required):
            required = step
        log(f"- {step:5} {fid}: {entry.get('text', '')}")
    for fid in sorted(set(exceptions) - used):
        annotate("notice", "api-contract exception", f"exception '{fid}' matched no finding; remove it when it is no longer needed")
    declared_step = step_between(released, declared)
    outcome.outputs.update({"required-step": required, "declared-step": declared_step,
                            "findings": json.dumps(sorted({fid for fid, _, _ in counted}))})
    risky = [fid for fid, _, e in counted if e.get("level") == 2]
    breaking = [fid for fid, step, _ in counted if step == "major"]
    if risky:
        annotate("warning", "api-contract risky change",
                 "risky (needs a recorded behavioural approval and a changelog entry, GACS-1 §8.5): " + "; ".join(risky))
    if breaking:
        annotate("warning", "api-contract breaking change",
                 "breaking (needs every artifact in required_for_breaking, GACS-1 §8.6): " + "; ".join(breaking))
    if STEPS.index(declared_step) < STEPS.index(required):
        outcome.fail("version-step-insufficient",
                     f"this change needs at least a {required} version step from the last release {latest_version}; "
                     f"info.version {declared_raw} is a {declared_step} step")
    else:
        outcome.passes.append(f"{latest_version} -> {declared_raw} is a {declared_step} step; the change needs {required}")


# --------------------------------------------------------------------------- sdk + lockfile


def require_clean_tree() -> None:
    status = git("status", "--porcelain", "--untracked-files=all")
    if status.strip():
        raise Refusal("dirty-worktree", "the working tree is not clean before regeneration:\n" + status)


def run_command(command: str, label: str, code: str) -> None:
    log(f"$ {label}: {command}")
    proc = subprocess.run(["bash", "-euo", "pipefail", "-c", command], capture_output=True, text=True)
    if proc.stdout.strip():
        log(proc.stdout[-4000:])
    if proc.returncode != 0:
        raise Refusal(code, f"{label} command failed (exit {proc.returncode}): {proc.stderr.strip()[-2000:]}")


def check_sdk(policy: Policy, outcome: Outcome) -> None:
    if policy.sdk is None:
        raise Refusal("sdk-not-configured", f"{policy.path} at the base names no sdk; do not run this check, or configure it")
    require_clean_tree()
    output = str(repo_path(policy.sdk["output"], "sdk.output"))
    if "install" in policy.sdk:
        run_command(policy.sdk["install"], "sdk.install", "sdk-install-failed")
    target = Path(output)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    run_command(policy.sdk["generate"], "sdk.generate", "sdk-generate-failed")
    drift = git("status", "--porcelain", "--untracked-files=all", "--", output)
    if drift.strip():
        diff = git("diff", "--stat", "--", output)
        raise Refusal("sdk-drift",
                      f"regenerating {output} from the contract does not reproduce the committed files. Generated "
                      f"code is not edited (GACS-1 §6.3): regenerate and commit.\n{drift}{diff}")
    if not git("ls-files", "--", output).strip():
        raise Refusal("sdk-empty", f"{output} is empty after generation and nothing is committed there: nothing was proven")
    outcome.passes.append(f"{output} regenerates byte-for-byte from the contract")


def check_lockfile(policy: Policy, outcome: Outcome) -> None:
    if policy.sdk is None:
        raise Refusal("sdk-not-configured", f"{policy.path} at the base names no sdk generator, so there is no generator lockfile to check")
    lockfile = str(repo_path(policy.sdk["lockfile"], "sdk.lockfile"))
    if git_ok("ls-files", "--error-unmatch", "--", lockfile).returncode != 0 or not Path(lockfile).is_file():
        raise Refusal("lockfile-missing", f"the generator lockfile {lockfile} is not committed (GACS-1 §6.4: the generator is pinned)")
    require_clean_tree()
    if "install" in policy.sdk:
        run_command(policy.sdk["install"], "sdk.install", "lockfile-install-failed")
        if git("status", "--porcelain", "--", lockfile).strip():
            raise Refusal("lockfile-drift",
                          f"installing the generator toolchain changed {lockfile}: it does not pin what is installed\n"
                          + git("diff", "--", lockfile)[-3000:])
        outcome.passes.append(f"{lockfile} is committed and installing from it leaves it unchanged")
    else:
        outcome.passes.append(f"{lockfile} is committed (name sdk.install to prove it installs unchanged)")


# --------------------------------------------------------------------------- main


def write_results(outcome: Outcome) -> None:
    result = "pass" if outcome.ok else "fail"
    reasons = sorted({code for code, _ in outcome.failures}) or ["ok"]
    outputs = {"result": result, "reasons": " ".join(reasons), **outcome.outputs}
    for code, message in outcome.failures:
        annotate("error", f"api-contract {outcome.check}: {code}", message)
    for line in outcome.passes:
        log(f"PASS {line}")
    print(f"api-contract {outcome.check}: {result.upper()} ({' '.join(reasons)})")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value.replace(chr(10), ' ')}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(f"### api-contract / {outcome.check}: {result.upper()}\n\n")
            for key in ("baseline", "required-step", "declared-step"):
                if key in outputs:
                    handle.write(f"- {key}: `{outputs[key]}`\n")
            for code, message in outcome.failures:
                handle.write(f"- **{code}**: {message.splitlines()[0][:500]}\n")
            for line in outcome.passes:
                handle.write(f"- {line}\n")
            handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", required=True, choices=CHECKS)
    args = parser.parse_args(argv)
    outcome = Outcome(args.check)
    first_release_raw = os.environ.get("API_CONTRACT_FIRST_RELEASE", "false").strip()
    tmp_root = os.environ.get("RUNNER_TEMP") or None
    with tempfile.TemporaryDirectory(prefix="api-contract-", dir=tmp_root) as tmp:
        workdir = Path(tmp)
        try:
            if first_release_raw not in ("true", "false"):
                raise Refusal("input-invalid", f"first-release must be 'true' or 'false', not {first_release_raw!r}")
            toolchain()
            policy = load_policy(workdir)
            if args.check == "validate":
                check_validate(policy, outcome, workdir)
            elif args.check == "lint":
                check_lint(policy, outcome, workdir)
            elif args.check == "compatibility":
                check_compatibility(policy, outcome, workdir, first_release_raw == "true")
            elif args.check == "sdk":
                check_sdk(policy, outcome)
            else:
                check_lockfile(policy, outcome)
        except Refusal as refusal:
            outcome.fail(refusal.code, refusal.message)
        except Exception as exc:  # noqa: BLE001 - an unexpected error is a failure, never a pass
            outcome.fail("internal-error", f"{type(exc).__name__}: {exc}")
    write_results(outcome)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
