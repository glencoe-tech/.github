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
- Couple material architecture, contract, security, persistence, failure, recovery, operational, and
  deployment changes with the relevant durable documentation and executable proof.
- Prefer tests, schemas, types, policy, validation, and generated references over duplicated prose.
- Preserve repository-specific architecture and conventions; standardize behavior, not filenames.
- Stop after acceptance criteria and proportionate verification pass. Report genuine blockers plainly.

Final responses use `RESULT: PASS | PARTIAL | BLOCKED | FAIL`, then concise `CHANGES`, `PROOF`,
`DOCUMENTATION`, `ARCHITECTURE`, and `RISKS / REMAINING` sections.
<!-- ODG-UNIVERSAL-AGENT-STANDARD:END -->
