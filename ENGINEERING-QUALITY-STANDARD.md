# Glencoe Technology Engineering Quality Standard

**The quality contract every glencoe-tech repository inherits. This is the source of truth for
how code ships, how infrastructure exists, and what CI must prove — for every engineering team.**

> Status: **ADOPTED** — mandatory for all repos in the org.
> Version: **1.0.0**. Changes land by maintainer-approved PR to this repo.
> Lineage: adapted from the AvenraCloud Engineering Quality Standard v1.x (`AvenraCloud/.github`),
> which remains the reference for gate implementation detail; where the two disagree for a
> Glencoe repo, this document wins.

## 1. The contract

1. **Fail closed.** A gate that cannot run (missing tool, missing config, empty input set where
   files are expected) fails the build with a legible error. A check that passes having verified
   nothing is a defect, not a pass.
2. **Zero warning debt on the gated surface.** Gated tools run warning-clean; new or modified
   code merges only with zero unreviewed findings.
3. **No suppression to get green.** Never disable, skip, or path-exclude a check merely to make
   CI pass. Exceptions are narrow, justified in-file, owned, and time-bounded.
4. **One canonical command.** Each repo's quality gates run identically locally and in CI.
5. **Pin everything executable.** Actions by tag or SHA, tools by version, providers by exact
   version + committed lockfile. No mutable `latest` in CI.
6. **Secrets never enter the repo.** No tokens, keys, or credential exports in source, fixtures,
   or docs — including "temporary" ones. Credential files found in a repo are treated as exposed:
   rotate first, delete second.
7. **PRs, not pushes.** Default branches are protected; changes land by reviewed PR. Applies and
   deploys that mutate production are additionally gated by a `production` environment with
   required reviewers.
8. **Report honestly.** A merge is not a delivery; a plan is not an apply; absence of a finding
   is never proof. Claims about live systems carry evidence (a run link, an enumeration), not
   inference from the repo.
9. **Language strategy.** TypeScript for web surfaces, Python for automation/services, Go where
   performance or trust demands it. Declarative config (HCL/YAML) is not a substitute for tested
   code. New languages need a recorded decision.
10. **Infrastructure and policy code are production code.** Terraform, workflows, and policy
    definitions get the same review and gate discipline as application code.
11. **Infrastructure exists only through IaC.** Every cloud resource the org depends on —
    Cloudflare (zones, DNS, tunnels, Access, R2, Workers/Pages configuration), and any future
    provider — is owned by a Terraform root in a version-controlled repo with remote state,
    pinned providers, plan-on-PR, and human-gated applies. Creating or mutating a resource via a
    provider dashboard, raw API call, or one-off script is a policy violation. The only
    sanctioned exceptions: day-0 bootstrap that is imported into Terraform immediately after,
    and provider gaps documented in the owning repo. **App-layer deploys are the carve-out**:
    `wrangler` deploying Worker/Pages *code*, and Pages git-integration *builds*, stay app
    deploys — the routes, custom domains, DNS, bindings, and project objects around them are
    Terraform-owned. A resource discovered outside IaC is adopted by an **import PR** whose
    first plan shows no changes — never mirrored by hand. Live coverage is proven by read-only
    account recon (`cloudflare-recon` in `AvenraCloud/avenra-infra` covers the shared account),
    because the account — not any repo — is the source of truth for what exists.

## 2. Repo tiers — the same contract, gates sized to the surface

Every repo declares its tier in its README. The contract (§1) applies to all tiers; the CI gate
set scales:

| Tier | Examples | Required CI gates |
|---|---|---|
| **infra** | `glencoe-infra`, `axentra-infra` | fmt + validate + plan-on-PR; apply only via gated environment; destroy-guard; lockfiles committed |
| **product** | `axentra-core`, `axentra-atlas`, `glencoe-platform` | build + typecheck/lint + tests on PR; secret scanning; pinned toolchain; reviewed deploys |
| **site** | marketing/client sites (`odg-web`, `360-grill`, `the-votex`, …) | build must succeed on PR; secret scanning. Deploys may remain Pages git-integration (an app deploy under rule 11) — but the Pages **project and domains** are Terraform-owned in `glencoe-infra` |

A repo with **no CI at all is out of contract** — the minimum for any tier is a PR build check
plus secret scanning.

## 3. Cloudflare specifics (the shared account)

- The Glencoe Technology Cloudflare account is shared with the Avenra platform. Ownership is
  recorded in `AvenraCloud/avenra-infra` `scripts/cloudflare-ownership.json`; Glencoe zones,
  Pages projects, and client R2 belong to `glencoe-tech/glencoe-infra`; `axentra.app` belongs to
  `glencoe-tech/axentra-infra`.
- Never mint broad account tokens for a single job; scope tokens to the zones/surfaces the job
  touches, and store them only as repo/environment secrets.
- Zone security posture (TLS floor, always-use-HTTPS, DNSSEC) is owned by the zone's Terraform
  repo — dashboard toggling is a rule-11 violation even for "quick fixes".

## 4. Adoption path for existing repos

1. Declare the tier in the README.
2. Add the minimum gates for the tier (PR build + secret scan is one workflow file).
3. Anything the repo creates in a cloud account moves behind Terraform (rule 11) — the owning
   infra repo adopts it via import PR.
4. Repos that cannot meet their tier yet record the gap and an owner in their README — visible
   debt, never silent.
