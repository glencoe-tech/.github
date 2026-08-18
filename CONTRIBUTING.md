# Contributing — glencoe-tech

Applies to every repo in the org. The quality contract lives in
[ENGINEERING-QUALITY-STANDARD.md](ENGINEERING-QUALITY-STANDARD.md); this is the working agreement
around it.

## The flow

1. Branch from `main`; changes land by PR. No direct pushes to default branches.
2. CI must be green — and green must mean something: for infra repos, read the **rendered plan**
   on the PR, not just the HCL diff. A plan line of `N to import, M to change` with `M > 0` on an
   adoption PR must not be merged.
3. Production mutations (Terraform applies, deploys beyond preview) go through the `production`
   environment with required reviewers.
4. Commit messages say **why**, not just what. If a change was forced by a live incident or a
   provider quirk, the message records it — that is where the next engineer finds it.

## Hard rules

- **No dashboard infrastructure.** If you need a DNS record, tunnel, bucket, domain binding, or
  Access rule: PR to the owning Terraform repo (`glencoe-infra`, `axentra-infra`, or
  `AvenraCloud/avenra-infra` for platform surfaces). Standard §1 rule 11.
- **No secrets in repos, ever** — including pasted tokens in issues/PR bodies. A leaked
  credential is rotated first and cleaned up second.
- **No legacy left behind.** Migrations reproduce properly, cut over, then delete — never rename
  or band-aid.
- **No `ubuntu-latest` in a private repo.** New jobs declare `runs-on: [self-hosted, avenra-ci]`.
  The org's Actions budget is pinned at `$0`, so a GitHub-hosted job does not just cost money —
  it dies in seconds with zero steps executed. Standard §2, *Where those gates run*, lists the
  handful of real exceptions (public repos, fork-PR arms, `harden-runner`).

## Getting a repo into contract

Add the tier declaration + minimum workflow (PR build + secret scan) from the standard's §2/§4.
When in doubt about a gate, copy the closest in-contract repo of the same tier.

New or copied workflows also need `.github/actionlint.yaml` declaring the runner label, or
actionlint fails the PR with `label "avenra-ci" is unknown`:

```yaml
self-hosted-runner:
  labels:
    - avenra-ci
```

This repo is one of the org's two public repos, so **its own** workflows stay on GitHub-hosted
runners (free minutes; the appliance deliberately does not serve public repos). That is an
exception to document, not a pattern to copy into a private repo.
