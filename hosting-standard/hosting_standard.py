#!/usr/bin/env python3
"""Right-sized hosting check (glencoe-tech/governance docs/HOSTING-COST-STANDARD.md).

    python3 hosting_standard.py [repo-root]

Passes a repository with no Cloudflare config. Otherwise:
  1. `run_worker_first: true` is not allowed (use a path list such as ["/api/*"]).
  2. README.md carries a `## Hosting` record naming a tier: T0, T1 or T2.
  3. The configuration matches the tier:
       T0  no Worker script (no `main`; a Pages project has no functions/)
       T1  no server runtime (OpenNext); a script in front of static assets runs only on paths
       T2  names the requirement that needs rendering
Standard library only; exits 1 with every problem listed.
"""
import json, os, re, sys

SKIP = {"node_modules", ".git", ".next", ".open-next", ".wrangler", "out", "dist", "worktrees"}
CONFIGS = ("wrangler.jsonc", "wrangler.json", "wrangler.toml")


def jsonc(text):
    text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def load(path):
    text = open(path, encoding="utf-8").read()
    if path.endswith(".toml"):
        import tomllib
        return tomllib.loads(text)
    return jsonc(text)


def configs(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP and not x.startswith("."))
        for name in CONFIGS:
            if name in files:
                yield os.path.join(d, name)
                break


def uses_opennext(directory, cfg):
    if str(cfg.get("main", "")).startswith(".open-next"):
        return True
    try:
        pkg = json.load(open(os.path.join(directory, "package.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "@opennextjs/cloudflare" in {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}


def record(root):
    try:
        readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"^## Hosting\s*$(.*?)(?=^## |\Z)", readme, re.M | re.S)
    if not m:
        return None
    body = m.group(1)
    tier = re.search(r"\*\*Tier:\*\*\s*(T[012])\b", body)
    need = re.search(r"\*\*Requirements that need code:\*\*\s*(.+)", body)
    return {"tier": tier.group(1) if tier else None, "need": (need.group(1).strip() if need else "")}


def check(root):
    problems, found = [], list(configs(root))
    if not found:
        return [], "no Cloudflare config: not a Cloudflare web repository"
    rec = record(root)
    if rec is None:
        problems.append("README.md has no `## Hosting` record (docs/HOSTING-COST-STANDARD.md)")
    elif rec["tier"] is None:
        problems.append("the `## Hosting` record names no tier (`- **Tier:** T0`, `T1` or `T2`)")
    for path in found:
        rel = os.path.relpath(path, root)
        try:
            cfg = load(path)
        except Exception as error:  # noqa: BLE001 - report, never crash the gate
            problems.append(f"{rel}: cannot be read ({error})")
            continue
        assets = cfg.get("assets") or {}
        rwf = assets.get("run_worker_first")
        if rwf is True:
            problems.append(f'{rel}: run_worker_first is true; use a path list such as ["/api/*"]')
        tier = rec and rec["tier"]
        if not tier:
            continue
        directory = os.path.dirname(path)
        script = bool(cfg.get("main")) or os.path.isdir(os.path.join(directory, "functions"))
        if tier == "T0" and script:
            problems.append(f"{rel}: tier T0 but a Worker script runs (`main` or functions/)")
        if tier in ("T0", "T1") and uses_opennext(directory, cfg):
            problems.append(f"{rel}: tier {tier} but uses a server runtime (OpenNext); that is T2")
        if tier == "T2" and len(rec["need"]) < 10:
            problems.append("tier T2 must name the requirement that needs rendering")
    return problems, f"{len(found)} Cloudflare config(s), tier {rec['tier'] if rec else 'none'}"


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    problems, summary = check(root)
    print(f"hosting standard: {summary}")
    for problem in problems:
        print(f"::error::{problem}")
    sys.exit(1 if problems else 0)
