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
12. **CI runs on the shared runner appliance.** Every new job in a private repo declares
    `runs-on: [self-hosted, avenra-ci]`. GitHub-hosted runners are the exception, not the
    default, and each exception is one of the narrow cases in §2 — not a preference. See §2,
    *Where those gates run*.

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

### Where those gates run

Gates run on the shared `avenra-ci` runner appliance, not on GitHub-hosted runners:

```yaml
jobs:
  build:
    runs-on: [self-hosted, avenra-ci]
```

Two things make this a hard default rather than a cost preference:

- **`ubuntu-latest` does not merely cost money in this org — it does not run.** glencoe-tech's
  included Actions minutes are exhausted and the org's Actions budget is pinned at `$0` with
  `prevent_further_usage`. A job on a GitHub-hosted runner fails in seconds with a spending-limit
  error, having executed zero steps. A workflow authored the old way is a broken workflow.
- The appliance absorbs the estate's real compute at flat cost. It is defined in
  `AvenraCloud/avenra-infra` at `infra/ci-runner/` and documented in that repo's
  [`docs/SELF-HOSTED-RUNNERS.md`](https://github.com/AvenraCloud/avenra-infra/blob/main/docs/SELF-HOSTED-RUNNERS.md)
  — the authority for capacity, security model and operations. It is shared by `glencoe-tech`,
  `serenoty-labs` and `AvenraCloud`, with a small number of concurrent slots per org, so keep
  jobs short and `timeout-minutes` honest.

Each repo also needs `.github/actionlint.yaml` declaring the label, or the actionlint gate fails
with `label "avenra-ci" is unknown`:

```yaml
self-hosted-runner:
  labels:
    - avenra-ci
```

**Standing exceptions — these legitimately stay on GitHub-hosted runners:**

- **Public repos** (`glencoe-tech/.github`, `glencoe-tech/talos`). Their minutes are free, and
  running fork-PR code on a shared box is a real security problem — the appliance's runner group
  does not serve public repos at all.
- **Fork-PR safety arms** in reusable workflows that deliberately force `ubuntu-latest` for
  `pull_request` events from forks.
- **Jobs needing a hosted-only runner feature**, notably `step-security/harden-runner`, which
  only functions on GitHub-hosted runners.

> **Temporary overlay (through 2026-09-01).** Credential-bearing and production-critical lanes —
> Terraform plan/apply, production migrations, production promote/deploy, daily backups, GitHub
> App token jobs — normally stay on GitHub-hosted runners, because the appliance must never see a
> platform credential. While the budget cap is in force they are ported onto `avenra-ci` as an
> explicit, time-boxed, risk-accepted exception; each ported line carries a `TEMP-HOSTED-PORT`
> marker and reverts when the billing cycle resets. Checklist:
> [`glencoe-tech/governance#10`](https://github.com/glencoe-tech/governance/issues/10). Do not
> copy a `TEMP-HOSTED-PORT` line as a pattern for new credential-bearing work.

## 3. Cloudflare specifics (the shared account)

- The Glencoe Technology Cloudflare account is shared with the Avenra platform. Ownership is
  recorded in `AvenraCloud/avenra-infra` `scripts/cloudflare-ownership.json`; Glencoe zones,
  Pages projects, and client R2 belong to `glencoe-tech/glencoe-infra`; `axentra.app` belongs to
  `glencoe-tech/axentra-infra`.
- Never mint broad account tokens for a single job; scope tokens to the zones/surfaces the job
  touches, and store them only as repo/environment secrets.
- Zone security posture (TLS floor, always-use-HTTPS, DNSSEC) is owned by the zone's Terraform
  repo — dashboard toggling is a rule-11 violation even for "quick fixes".

## 4. Enforcement — governance-as-code across the whole group

This standard is not advisory: it is enforced mechanically by
[`glencoe-tech/governance`](https://github.com/glencoe-tech/governance) — the group's
governance-as-code root (the `glencoe-governance` GitHub App + OpenTofu), which applies org
rulesets, Actions hardening, security defaults, and repo settings across **every owned org**:
`glencoe-tech`, `serenoty-labs`, `AvenraCloud`, `screenmind`, `zerolatencylabs`
(and `peeps-africa` once the App is installed there — currently a recorded gap).

- Sister-company and client repos inherit the contract the same way engineering repos do —
  landing in a governed org **is** opting into this standard.
- A governance change that would let a repo drop below its tier is a change to this document
  first; governance config never silently weakens the contract.
- The AvenraCloud org additionally carries its own stricter EQS; governance enforces the union,
  never the weaker of the two.

## 5. Adoption path for existing repos

1. Declare the tier in the README.
2. Add the minimum gates for the tier (PR build + secret scan is one workflow file).
3. Anything the repo creates in a cloud account moves behind Terraform (rule 11) — the owning
   infra repo adopts it via import PR.
4. Repos that cannot meet their tier yet record the gap and an owner in their README — visible
   debt, never silent.
