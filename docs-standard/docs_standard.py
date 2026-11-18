#!/usr/bin/env python3
"""Deterministic documentation checks behind GEDS-001 (glencoe-tech/governance).

Objective findings (D1xx) block only in `enforce` mode. Heuristic findings (W2xx) are advisory in
every mode and never block. Standard-library Python only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_KEYS = {
    "version", "doc_paths", "ignore", "governed", "decision_records", "high_impact",
    "protected", "exceptions", "deferrals", "uncovered",
}
RULE_KEYS = {"id", "paths", "docs", "critical"}
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc")
DEFAULT_DOC_PATHS = [
    "README.md", "**/README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md",
    "CHANGELOG.md", "docs/**", ".github/**",
]
# Templates hold placeholders, not links or real metadata. Always ignored, in addition to config.
DEFAULT_IGNORE = ["**/templates/**", "**/*[Tt][Ee][Mm][Pp][Ll][Aa][Tt][Ee].md"]
DEFAULT_GOVERNED = [
    {"glob": "**/adr/*.md", "class": "adr", "fields": ["status"]},
    {"glob": "**/*[Rr][Uu][Nn][Bb][Oo][Oo][Kk]*.md", "class": "runbook", "fields": ["owner"]},
]
DEFAULT_DECISION_RECORDS = [{"glob": "**/adr/*.md", "id": r"^(\d{3,5})-"}]
ADR_STATUSES = {"proposed", "accepted", "superseded", "deprecated", "rejected"}
EXCEPTION_KEYS = {"code", "path", "reason", "owner", "risk", "expires", "replacement"}
MAX_EXCEPTION_DAYS = 180
# A deferral postpones one rule's documentation to a named issue. It lives in the configuration, so it
# is reviewed as a diff and expires; a PR description alone never defers anything.
DEFERRAL_KEYS = {"rule", "issue", "reason", "owner", "expires"}
MAX_DEFERRAL_DAYS = 30
# Findings that an exception may never suppress: deferrals replace exceptions for D104, and an expiry
# or a broken mapping cannot be excepted away.
UNEXCEPTABLE = {"D104", "D107", "D108", "D109"}
# Words that carry no reason on their own. A `none` reason needs MIN_REASON_WORDS words beyond these.
GENERIC_WORDS = {
    "a", "an", "and", "applicable", "as", "change", "changed", "changes", "diff", "doc", "docs",
    "documentation", "explanatory", "fix", "for", "impact", "in", "internal", "is", "it", "just",
    "later", "minor", "n", "na", "need", "needed", "no", "none", "not", "nothing", "of", "on", "only",
    "or", "pr", "refactor", "see", "self", "small", "tbd", "the", "this", "to", "todo", "trivial",
}
MIN_REASON_WORDS = 4

IMPACT_LINE = re.compile(
    r"^[\s>*_-]*documentation impact[\s*_]*:[\s*_]*(none|updated|follow-up)\b(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
ISSUE_REF = re.compile(r"(?:[\w.-]+/[\w.-]+)?#\d+|https://github\.com/\S+/issues/\d+")
FIELD_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}(owner|status|version)\*{0,2}\s*:\s*\*{0,2}\s*(.*?)\s*$",
    re.IGNORECASE,
)
LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'(][^)]*)?\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=[\"']([^\"']+)[\"']", re.IGNORECASE)
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

COMMENT_PREFIX = {
    ".py": "#", ".sh": "#", ".tf": "#", ".hcl": "#", ".yml": "#", ".yaml": "#", ".rb": "#",
    ".ts": "//", ".tsx": "//", ".js": "//", ".jsx": "//", ".mjs": "//", ".go": "//",
    ".rs": "//", ".java": "//", ".kt": "//", ".swift": "//", ".c": "//", ".cc": "//",
}
# Commented-out code: only strict statement shapes count, so prose that starts with "if", "from" or
# "return", or documents a mapping like `KEYS[1] = bucket key`, is left alone.
KEYWORD_CODE = re.compile(
    r"^(?:return\b(?:\s+\S+){0,3};?$|(?:if|elif|while|for)\s*\(.*\)\s*\{?$|(?:if|elif|while)\s.+:$"
    r"|for\s+\w+(?:,\s*\w+)*\s+in\s+.+:$|else\s*[:{]$|(?:def|func)\s+\w+\(|class\s+\w+[(:{]"
    r"|(?:const|let|var)\s+\w+\s*[:=]|import\s+[\w{*]|from\s+[\w.]+\s+import\s|await\s+[\w.]+\("
    r"|print\(|console\.\w+\()"
)
CODE_SHAPE = re.compile(
    r"(?:^[\w.\[\]]+\s*[+\-*/]?=\s*(?:\S+|.*[)\]}])\s*;?$|^[\w.]+\(.*\)\s*;?$|^[})\]]+[;,]?$"
    r"|^.*(?:\)|=>)\s*\{$)"
)
# Change history: a comment that opens with a date or version, or credits a change to a person.
HISTORY = re.compile(
    r"(?:^\[?(?:20\d\d-\d\d-\d\d|v\d+(?:\.\d+)+)\]?\s*[:\-–—]|\b(?:changed|modified|updated|fixed|added"
    r"|removed)\s+by\s+@[\w-]+)",
    re.IGNORECASE,
)
DECORATION = re.compile(r"[─━═]{2,}|[-=*#]{4,}")
INTENT_WORDS = re.compile(
    r"\b(?:because|why|must|never|always|only|note|warning|invariant|security|todo|fixme|hack"
    r"|safe|unsafe|assume|assumes|otherwise|so that|until|unless|see)\b",
    re.IGNORECASE,
)
STOPWORDS = {
    "the", "a", "an", "to", "of", "and", "for", "in", "on", "with", "from", "this", "that", "is",
    "it", "we", "new", "then", "into", "by", "as", "at", "be", "our",
}
DIARY = re.compile(
    r"(?:^|[_-])(?:report|summary|complete|completed|findings|session|notes|handoff|status"
    r"|progress|done|changes|log)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    line: int
    message: str

    @property
    def blocking(self) -> bool:
        return self.code.startswith("D")


class ConfigError(ValueError):
    pass


@lru_cache(maxsize=None)
def glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i:]:
            end = pattern.index("]", i)
            out.append(pattern[i:end + 1])
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    return any(glob_regex(p).match(path) for p in patterns)


def is_doc(path: str, config: dict) -> bool:
    return path.lower().endswith(DOC_SUFFIXES) and matches(path, config["doc_paths"])


def load_config(root: Path, relative: str) -> dict:
    path = root / relative
    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{relative}: top level must be an object")
    unknown = set(raw) - CONFIG_KEYS
    if unknown:
        raise ConfigError(f"{relative}: unknown keys {sorted(unknown)}")
    config = {
        "path": relative,
        "doc_paths": raw.get("doc_paths", DEFAULT_DOC_PATHS),
        "ignore": DEFAULT_IGNORE + raw.get("ignore", []),
        "governed": raw.get("governed", DEFAULT_GOVERNED),
        "decision_records": raw.get("decision_records", DEFAULT_DECISION_RECORDS),
        "high_impact": raw.get("high_impact", []),
        "protected": raw.get("protected", []),
        "exceptions": raw.get("exceptions", []),
        "deferrals": raw.get("deferrals", []),
        "uncovered": raw.get("uncovered", []),
    }
    ids = set()
    for rule in config["high_impact"]:
        if not {"id", "paths", "docs"} <= set(rule) or set(rule) - RULE_KEYS:
            raise ConfigError(f"{relative}: high_impact rules need id, paths and docs (and optionally critical)")
        if not isinstance(rule.get("critical", False), bool):
            raise ConfigError(f"{relative}: high_impact '{rule['id']}': critical must be true or false")
        if rule["id"] in ids:
            raise ConfigError(f"{relative}: high_impact id '{rule['id']}' is used twice")
        ids.add(rule["id"])
    for item in config["exceptions"]:
        if set(item) != EXCEPTION_KEYS:
            raise ConfigError(f"{relative}: exceptions need exactly {sorted(EXCEPTION_KEYS)}")
        if item["code"] in UNEXCEPTABLE:
            raise ConfigError(f"{relative}: {item['code']} cannot be excepted (use deferrals for D104)")
        dt.date.fromisoformat(item["expires"])
    for item in config["deferrals"]:
        if set(item) != DEFERRAL_KEYS:
            raise ConfigError(f"{relative}: deferrals need exactly {sorted(DEFERRAL_KEYS)}")
        if item["rule"] not in ids:
            raise ConfigError(f"{relative}: deferral names unknown high_impact rule '{item['rule']}'")
        if not ISSUE_REF.fullmatch(item["issue"]):
            raise ConfigError(f"{relative}: deferral issue must be owner/repo#n, #n or an issue URL")
        dt.date.fromisoformat(item["expires"])
    for item in config["uncovered"]:
        if set(item) != {"area", "reason"} or not all(str(v).strip() for v in item.values()):
            raise ConfigError(f"{relative}: uncovered entries need a non-empty area and reason")
    return config


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def tracked_files(root: Path) -> list[str]:
    return [p for p in git(root, "ls-files", "-z").split("\0") if p]


def changed_files(root: Path, base: str) -> dict[str, str]:
    """Map each changed path to A, M or D. A rename deletes its source and adds its target."""
    changes: dict[str, str] = {}
    fields_ = git(root, "diff", "--name-status", "-M", "-z", f"{base}...HEAD").split("\0")
    i = 0
    while i < len(fields_) - 1:
        status = fields_[i][0]
        if status in "RC":
            if status == "R":
                changes[fields_[i + 1]] = "D"
            changes[fields_[i + 2]] = "A"
            i += 3
        else:
            changes[fields_[i + 1]] = "M" if status == "T" else status
            i += 2
    return changes


def added_lines(root: Path, base: str, path: str) -> list[tuple[int, str]]:
    out = git(root, "diff", "-U0", "--no-color", f"{base}...HEAD", "--", path)
    lines, number = [], 0
    for row in out.splitlines():
        hunk = re.match(r"^@@ -\S+ \+(\d+)", row)
        if hunk:
            number = int(hunk.group(1))
        elif row.startswith("+") and not row.startswith("+++"):
            lines.append((number, row[1:]))
            number += 1
    return lines


def read_text(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8", errors="replace")


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Lines outside fenced code blocks, with inline code spans removed."""
    rows, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        opener = FENCE.match(line)
        if opener:
            mark = opener.group(1)[0]
            fence = None if fence == mark else (fence or mark)
            continue
        if fence is None:
            rows.append((number, re.sub(r"`+[^`]*`+", "", line)))
    return rows


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).lower()
    text = "".join(ch for ch in text if ch.isalnum() or ch in " -_")
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in prose_lines(text):
        found.update(a.lower() for a in HTML_ANCHOR.findall(line))
        heading = HEADING.match(line)
        if heading:
            base = slug(heading.group(1))
            n = counts.get(base, 0)
            counts[base] = n + 1
            found.add(base if n == 0 else f"{base}-{n}")
    return found


class Tree:
    """The tracked files of the checked revision, with directory and anchor lookups cached."""

    def __init__(self, root: Path, files: list[str]):
        self.root = root
        self.files = set(files)
        self.dirs = {str(parent) for f in files for parent in Path(f).parents if str(parent) != "."}
        self._anchors: dict[str, set[str]] = {}

    def exists(self, path: str) -> bool:
        return path in self.files or path.rstrip("/") in self.dirs

    def anchors(self, path: str) -> set[str]:
        if path not in self._anchors:
            self._anchors[path] = anchors(read_text(self.root, path))
        return self._anchors[path]


def check_links(tree: Tree, path: str) -> list[Finding]:
    findings = []
    for number, line in prose_lines(read_text(tree.root, path)):
        targets = [m.group(1) for m in LINK.finditer(line)]
        ref = REF_DEF.match(line)
        if ref:
            targets.append(ref.group(1))
        for raw in targets:
            target = raw.strip("<>")
            # A target with a space or angle bracket is a template placeholder, not a path.
            if not target or SCHEME.match(target) or target.startswith("//") or re.search(r"[\s<>]", target):
                continue
            file_part, _, fragment = target.partition("#")
            file_part = urllib.parse.unquote(file_part.split("?")[0])
            if file_part:
                base = Path("/") if file_part.startswith("/") else Path("/" + path).parent
                resolved = os.path.normpath(str(base / file_part)).lstrip("/")
            else:
                resolved = path
            if not tree.exists(resolved):
                findings.append(Finding("D101", path, number, f"link target does not exist: {target}"))
            elif fragment and resolved.lower().endswith(".md") and resolved in tree.files:
                if urllib.parse.unquote(fragment).lower() not in tree.anchors(resolved):
                    findings.append(Finding("D101", path, number, f"link anchor does not exist: {target}"))
    return findings


def fields(text: str) -> dict[str, str]:
    """Owner, Status and Version from YAML front matter or a bold-label header block."""
    head = text.splitlines()[:25]
    if head and head[0].strip() == "---":
        for line in list(head[1:]):
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                head.append(f"{key.strip()}: {value.strip()}")
    found: dict[str, str] = {}
    for line in head:
        match = FIELD_LINE.match(line)
        if match and match.group(1).lower() not in found:
            found[match.group(1).lower()] = match.group(2).replace("*", "").strip()
    return found


def check_metadata(root: Path, path: str, config: dict) -> list[Finding]:
    findings = []
    if Path(path).name.lower() in {"readme.md", "template.md"}:
        return findings
    for rule in config["governed"]:
        if not glob_regex(rule["glob"]).match(path):
            continue
        found = fields(read_text(root, path))
        for name in rule.get("fields", []):
            if not found.get(name):
                findings.append(Finding("D102", path, 1, f"{rule['class']} needs a '{name.title()}:' field"))
        word = re.match(r"[A-Za-z]+", found.get("status", ""))
        if rule["class"] == "adr" and word and word.group(0).lower() not in ADR_STATUSES:
            findings.append(Finding(
                "D102", path, 1, f"ADR status '{word.group(0)}' is not one of {sorted(ADR_STATUSES)}"
            ))
    return findings


def check_decision_ids(files: list[str], config: dict, changed: set[str] | frozenset = frozenset()) -> list[Finding]:
    """Changed files sort last, so a PR that reuses an id is told about its own file."""
    findings = []
    for rule in config["decision_records"]:
        pattern, ident = glob_regex(rule["glob"]), re.compile(rule["id"])
        seen: dict[tuple[str, str], str] = {}
        for path in sorted(files, key=lambda p: (p in changed, p)):
            match = ident.match(Path(path).name) if pattern.match(path) else None
            if not match:
                continue
            key = (str(Path(path).parent), match.group(1))
            if key in seen:
                findings.append(Finding(
                    "D103", path, 1, f"decision record id {match.group(1)} is also used by {seen[key]}"
                ))
            else:
                seen[key] = path
    return findings


def impact_declaration(body: str) -> tuple[str, str] | None:
    match = IMPACT_LINE.search(body)
    return (match.group(1).lower(), match.group(2).strip(" -—–:")) if match else None


def specific_reason(reason: str) -> bool:
    """A reason says something only when enough of its words are not stock phrases like "no impact"."""
    return len([w for w in re.findall(r"[a-z0-9]+", reason.lower()) if w not in GENERIC_WORDS]) >= MIN_REASON_WORDS


def active_deferrals(config: dict, today: dt.date) -> tuple[dict[str, dict], list[Finding]]:
    active, findings = {}, []
    for item in config["deferrals"]:
        expires = dt.date.fromisoformat(item["expires"])
        if expires < today:
            findings.append(Finding("D108", config["path"], 0, f"deferral for '{item['rule']}' expired on {expires}"))
        elif (expires - today).days > MAX_DEFERRAL_DAYS:
            findings.append(Finding("D108", config["path"], 0, f"deferral for '{item['rule']}' runs past {MAX_DEFERRAL_DAYS} days"))
        else:
            active[item["rule"]] = item
    return active, findings


def check_impact(changes: dict[str, str], body: str | None, config: dict, today: dt.date) -> list[Finding]:
    """A high-impact path that changed without its mapped docs needs a recorded reason.

    Changing a mapped document always satisfies a rule. Otherwise an active deferral in the
    configuration satisfies any rule, and `none — <specific reason>` satisfies a non-critical rule.
    Each accepted waiver or deferral is reported as W205, so a reviewer sees it. `updated` never
    stands in for the mapped documents, and `follow-up` in a description defers nothing.
    """
    active, findings = active_deferrals(config, today)
    fired = []
    for rule in config["high_impact"]:
        hits = [p for p in changes if matches(p, rule["paths"]) and not is_doc(p, config)]
        if hits and not any(matches(p, rule["docs"]) for p in changes):
            fired.append((rule, hits[0]))
    if not fired or body is None:
        return findings
    declared = impact_declaration(body)
    kind, detail = declared if declared else ("", "")
    for rule, hit in fired:
        docs = ", ".join(rule["docs"])
        critical = rule.get("critical", False)
        if rule["id"] in active:
            item = active[rule["id"]]
            findings.append(Finding("W205", hit, 0, f"documentation for '{rule['id']}' deferred to {item['issue']} until {item['expires']} ({item['owner']}): {item['reason']}"))
            continue
        if kind == "none" and not critical and specific_reason(detail):
            findings.append(Finding("W205", hit, 0, f"documentation for '{rule['id']}' waived in the PR description: {detail}"))
            continue
        if critical:
            problem = f"critical high-impact change ('{rule['id']}') needs its documentation ({docs}) in this PR, or an active deferral for it in {config['path']}"
        elif kind == "none":
            problem = f"'Documentation impact: none' for '{rule['id']}' needs a specific reason (at least {MIN_REASON_WORDS} words beyond stock phrases)"
        elif kind == "updated":
            problem = f"the documents updated are not the ones mapped for '{rule['id']}' ({docs}); update one of those, or correct the mapping in {config['path']}"
        elif kind == "follow-up":
            problem = f"a follow-up defers '{rule['id']}': record it under deferrals in {config['path']} (issue, owner, reason, expiry within {MAX_DEFERRAL_DAYS} days)"
        else:
            problem = f"high-impact change ('{rule['id']}') without its documentation ({docs}) or a 'Documentation impact:' line in the PR description"
        findings.append(Finding("D104", hit, 0, problem))
    return findings


def check_mappings(files: list[str], config: dict) -> list[Finding]:
    """A high-impact glob that matches no tracked file is a blind spot or a typo."""
    findings = []
    for rule in config["high_impact"]:
        for key in ("paths", "docs"):
            for pattern in rule[key]:
                if not any(glob_regex(pattern).match(f) for f in files):
                    findings.append(Finding("D109", config["path"], 0, f"high_impact '{rule['id']}' {key} pattern '{pattern}' matches no tracked file"))
    return findings


def check_protected(root: Path, base: str, changes: dict[str, str], config: dict) -> list[Finding]:
    findings = []
    for path, status in sorted(changes.items()):
        if not matches(path, config["protected"]):
            continue
        if status == "D":
            findings.append(Finding("D106", path, 0, "protected document deleted; supersede it instead"))
        elif status == "M":
            try:
                old = fields(git(root, "show", f"{base}:{path}")).get("version")
            except subprocess.CalledProcessError:
                old = None
            new = fields(read_text(root, path)).get("version")
            if not new or new == old:
                findings.append(Finding("D106", path, 1, "protected document changed without a new 'Version:'"))
    return findings


def comment_body(line: str, prefix: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith(prefix) or stripped.startswith(("#!", prefix + "/", prefix + "!")):
        return None
    return stripped[len(prefix):].strip()


def code_like(body: str) -> bool:
    return bool(KEYWORD_CODE.match(body) or CODE_SHAPE.match(body))


def words(text: str) -> list[str]:
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [w.lower() for w in re.findall(r"[A-Za-z]+", split)]


def check_comments(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    prefix = COMMENT_PREFIX.get(Path(path).suffix.lower())
    if not prefix:
        return []
    findings, run = [], []
    by_number = dict(lines)
    for number, line in lines + [(-1, "")]:
        body = comment_body(line, prefix)
        if body is not None and code_like(body):
            run.append(number)
            continue
        if len(run) >= 2:
            findings.append(Finding("W201", path, run[0], f"{len(run)} lines of commented-out code; delete them (Git keeps history)"))
        run = []
        if not body:
            continue
        if HISTORY.search(body):
            findings.append(Finding("W202", path, number, "change history in a comment; it belongs in the commit message"))
        following = by_number.get(number + 1)
        said = [w for w in words(body) if w not in STOPWORDS]
        if following and comment_body(following, prefix) is None and len(said) >= 2 and not DECORATION.search(body):
            if not INTENT_WORDS.search(body) and set(said) <= set(words(following)):
                findings.append(Finding("W203", path, number, "comment restates the next line; delete it or say why"))
    return findings


def check_new_docs(changes: dict[str, str], config: dict) -> list[Finding]:
    findings = []
    for path, status in sorted(changes.items()):
        if status != "A" or not path.lower().endswith(".md"):
            continue
        if DIARY.search(Path(path).stem):
            findings.append(Finding("W204", path, 1, "name reads like a task record; put lasting knowledge in an existing document"))
        elif not matches(path, config["doc_paths"]):
            findings.append(Finding("W204", path, 1, "new Markdown outside this repository's documentation locations"))
    return findings


def apply_exceptions(findings: list[Finding], config: dict, today: dt.date) -> list[Finding]:
    kept, active = [], []
    for item in config["exceptions"]:
        expires = dt.date.fromisoformat(item["expires"])
        if expires < today:
            kept.append(Finding("D107", item["path"], 0, f"exception for {item['code']} expired on {expires}"))
        elif (expires - today).days > MAX_EXCEPTION_DAYS:
            kept.append(Finding("D107", item["path"], 0, f"exception for {item['code']} runs past {MAX_EXCEPTION_DAYS} days"))
        else:
            active.append(item)
    for finding in findings:
        if not any(e["code"] == finding.code and matches(finding.path, [e["path"]]) for e in active):
            kept.append(finding)
    return kept


def run(root: Path, base: str | None, body: str | None, config: dict, today: dt.date) -> list[Finding]:
    every = tracked_files(root)
    tree = Tree(root, every)
    files = [p for p in every if not matches(p, config["ignore"])]
    markdown = [p for p in files if p.lower().endswith(".md")]
    if base is None:
        findings = check_decision_ids(files, config) + check_mappings(files, config)
        findings += active_deferrals(config, today)[1]
        for path in markdown:
            findings += check_links(tree, path) + check_metadata(root, path, config)
        return apply_exceptions(findings, config, today)
    changes = {p: s for p, s in changed_files(root, base).items() if not matches(p, config["ignore"])}
    findings = check_decision_ids(files, config, set(changes)) + check_mappings(files, config)
    live = sorted(p for p, s in changes.items() if s != "D")
    removed = [Path(p).name for p, s in changes.items() if s == "D"]
    scope = {p for p in live if p.lower().endswith(".md")}
    if removed:
        scope |= {p for p in markdown if any(name in read_text(root, p) for name in removed)}
    for path in sorted(scope):
        findings += check_links(tree, path)
        if changes.get(path) in {"A", "M"}:
            findings += check_metadata(root, path, config)
    findings += check_impact(changes, body, config, today)
    findings += check_protected(root, base, changes, config)
    findings += check_new_docs(changes, config)
    for path in live:
        findings += check_comments(path, added_lines(root, base, path))
    return apply_exceptions(findings, config, today)


def event_context(event_path: str | None) -> tuple[str | None, str | None]:
    """Base SHA and PR description from the event file. The description is data, never a command."""
    if not event_path or not Path(event_path).is_file():
        return None, None
    pull = json.loads(Path(event_path).read_text(encoding="utf-8")).get("pull_request")
    if not pull:
        return None, None
    return pull["base"]["sha"], pull.get("body") or ""


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(findings: list[Finding], mode: str, github: bool) -> str:
    lines = []
    for f in sorted(findings, key=lambda x: (x.code, x.path, x.line)):
        level = "error" if f.blocking and mode == "enforce" else ("notice" if f.code == "W205" else "warning")
        where = f"{f.path}:{f.line}" if f.line else f.path
        lines.append(f"{f.code} {level.upper()} {where} {f.message}")
        if github:
            location = f"file={escape(f.path)},line={f.line}," if f.line else ""
            lines.append(f"::{level} {location}title={f.code}::{escape(f.message)}")
    objective = sum(f.blocking for f in findings)
    lines.append(f"docs-standard: {objective} objective and {len(findings) - objective} advisory finding(s), mode {mode}")
    return "\n".join(lines)


def summary(findings: list[Finding], mode: str) -> str:
    rows = ["### Documentation standard (GEDS-001)", ""]
    if not findings:
        return "\n".join(rows + ["No findings."]) + "\n"
    rows += [f"Mode `{mode}`: objective findings (D) block only when enforced; advisory findings (W) never block.", "",
             "| Code | Location | Finding |", "|---|---|---|"]
    for f in sorted(findings, key=lambda x: (x.code, x.path, x.line)):
        where = f"{f.path}:{f.line}" if f.line else f.path
        rows.append(f"| {f.code} | `{where}` | {f.message.replace('|', '/')} |")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--base", help="base commit; without one the whole repository is scanned")
    parser.add_argument("--body-file", help="PR description to read the documentation impact line from")
    parser.add_argument("--mode", choices=["warn", "enforce"], default="warn")
    parser.add_argument("--config", default=".github/docs-standard.json")
    parser.add_argument("--github", action="store_true", help="read the PR event and emit annotations")
    parser.add_argument("--today", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    base, body = event_context(os.environ.get("GITHUB_EVENT_PATH")) if args.github else (None, None)
    base = args.base or base
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    try:
        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        findings = run(root, base, body, load_config(root, args.config), today)
    except (ConfigError, ValueError, KeyError) as error:
        print(f"docs-standard: configuration error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"docs-standard: git failed: {(error.stderr or '').strip()}", file=sys.stderr)
        return 2
    print(report(findings, args.mode, args.github))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.github and step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary(findings, args.mode))
    return 1 if args.mode == "enforce" and any(f.blocking for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
