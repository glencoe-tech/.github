<!-- ODG-UNIVERSAL-AGENT-STANDARD:START -->

# Universal agent contract

This contract applies to every automated engineering role in this repository: implementation,
infrastructure, platform, CI/CD, security, review, testing, incident, research, migration, release,
data, documentation, planning, operations, and multi-agent workflows.

Authority: [Universal Agent Operating Standard](https://github.com/glencoe-tech/governance/blob/main/docs/AGENT-OPERATING-STANDARD.md).
Repository instructions may be stricter; they may not weaken that standard.

- Begin with the task, current authoritative state, applicable decisions, affected code, and tests.
  Expand context only when uncertainty or consequence requires it.
- Optimize verified useful progress per compute, not minimum tokens. Avoid rediscovery, duplicate
  analysis, speculative work, irrelevant refactoring, needless escalation, and redundant handoffs.
- Investigate uncertainty with a falsifiable hypothesis and the cheapest trustworthy experiment.
- Keep scope bounded. Report unrelated debt; change it only when correctness or safety requires it.
- Evidence outranks assertion. Distinguish DECLARED, BUILT, TESTED, DEPLOYED, OBSERVED, and PROVEN.
- Agents document the system, not their activity. Do not add session diaries, completion reports,
  copied logs, or task-history Markdown.
- Read the documentation of what you change; update existing documents rather than adding new ones;
  state `Documentation impact:` in the PR; never claim documentation is published or verified without
  evidence. Rules: [GEDS-001](https://github.com/glencoe-tech/governance/blob/main/docs/ENGINEERING-DOCUMENTATION-STANDARD.md).
- Couple material architecture, contract, security, persistence, failure, recovery, operational, and
  deployment changes with the relevant durable documentation and executable proof.
- Prefer tests, schemas, types, policy, validation, and generated references over duplicated prose.
- **Know what an event triggers before you cause it.** Before pushing a branch or tag, opening or
  updating a pull request, arming auto-merge, or merging, read what that event runs: workflow
  triggers (including `pull_request_target`), path filters, `if:` conditions, environments, secrets,
  git integrations such as Workers Builds or Pages, and anything that acts on it elsewhere
  (`workflow_run`, dispatches, GitOps sync). If it can deploy, publish, apply infrastructure, restart
  a service, or give the job production credentials, stop and report it; proceed only on the
  accountable owner's explicit authorization for that event. A preview is safe only when no
  production secret reaches the job and it serves no production traffic. A missing target or an
  expected failure is not proof of safety.
- **Deployment secrets live in GitHub.** When GitHub runs a deployment, every secret it needs
  (runtime, build and the deploy credential) is a secret of the repository's GitHub environment for
  that target; a production environment admits only the default branch. They change
  only inside the deploy pipeline: for a Worker, sent with each release
  (`wrangler deploy --secrets-file`, which adds and overwrites but never deletes) and removed by
  `wrangler versions secret delete` between upload and deploy; a deploy-job step only for providers
  without that option (Pages direct upload, Supabase). Never `wrangler secret put` or `secret bulk`
  on a Worker, even in a pipeline: each deploys the last uploaded version. No change by hand, no
  dashboard edit. Builds Cloudflare runs from git (Workers Builds, Pages git builds, their previews)
  are exempt.
- Preserve repository-specific architecture and conventions; standardize behavior, not filenames.
- **Environment lifecycle is part of completion.** Use the declared Docker/container environment
  for dependency installation, builds, testing, and packaging. A necessary native/local fallback
  must be isolated, explained, and cleaned to the same standard.
- **Agent-created resources are task-scoped.** Establish ownership and teardown before creation.
  On success, failure, cancellation, or interruption, export requested deliverables and remove owned
  containers, disposable volumes, temporary images/build caches, VMs, worktrees, local environments,
  dependencies, and generated output. Preserve source, Git history, secrets, persistent data, and
  pre-existing/shared resources. Retention needs explicit authorization, an owner, and an expiry.
- **Verify teardown before PASS.** Use guaranteed cleanup handlers and expiry/reaping for resources
  that can outlive a job. Include cleanup evidence in PROOF; unresolved cleanup is PARTIAL/BLOCKED
  with the remaining resource, owner, expiry, and removal command in RISKS / REMAINING.
- Stop after acceptance criteria and proportionate verification pass. Report genuine blockers plainly.

Final responses use `RESULT: PASS | PARTIAL | BLOCKED | FAIL`, then concise `CHANGES`, `PROOF`,
`DOCUMENTATION`, `ARCHITECTURE`, and `RISKS / REMAINING` sections.

<!-- ODG-UNIVERSAL-AGENT-STANDARD:END -->
