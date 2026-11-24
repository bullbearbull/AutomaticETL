## Code Review Rules

### 1. Establish the intent and baseline behavior

- Read the PR description and any accessible linked issues to understand the
  purpose, acceptance criteria, and existing use cases that must continue to
  work. If requirements are unclear, use existing tests, public interfaces,
  CI configuration, and operational documentation to establish expected behavior.
- Compare the change against the PR's base branch. Look for changes in execution
  conditions, operation order, return values, and side effects that may be
  obscured by renaming, moving, or reformatting code.
- For a bug fix, verify that the new implementation addresses the original
  failure condition. Check its effect on normal paths, existing callers, and
  previously stored data.

### 2. Trace the impact beyond the changed files

- When arguments, return values, exceptions, defaults, state transitions, or
  data formats change, find the code that reads, writes, or consumes them.
  Include unchanged callers, wrappers, batch jobs, configuration loaders, and
  relevant tests.
- When direct call sites are unavailable, check relevant indirect entry points
  such as route registrations, callbacks, event subscriptions, serialization
  mappings, and configuration-driven dispatch.
- Trace each suspected impact from the entry point through the changed code to
  the consumer or storage layer. Identify the conditions that reach the changed
  path, any code that relies on the previous behavior, and any safeguards
  that preserve the expected outcome.
- In multi-file PRs, check the contracts between components. Verify that
  producers and consumers agree on units, field names, state values, and the
  meaning of errors.

### 3. Investigate failure modes relevant to the change

- **Return values, exceptions, and defaults.** Check whether missing values,
  empty values, zero, `false`, and failures carry different meanings. Trace
  paths that could record a failure as a valid result or suppress an existing
  alert.
- **Database writes, external requests, and batch jobs.** Trace what happens
  when a write succeeds but its response is lost, or when a partially completed
  job is retried. Check when transaction boundaries, unique constraints, and
  idempotency keys take effect. Where concurrent execution is supported,
  consider whether state can change between validation and persistence.
- **Schemas, APIs, and configuration.** Verify compatibility with existing
  data and older consumers. When old and new versions can coexist during
  deployment, check reads and writes before and after migration, along with
  supported rollback paths.
- **Aggregations, calculations, and time series.** Check units, signs,
  rounding, denominators, boundary values, time zones, and whether date
  boundaries are inclusive. Determine whether changes to duplicate keys,
  ordering, or missing-value handling alter the result. For time-dependent
  calculations, verify that each calculation uses only data available at
  the relevant decision time.
- **Feature flags and environment settings.** Check default values and
  configuration precedence. Verify the behavior in existing environments
  where a flag is off or a new setting is absent.
- **Test changes.** Verify that modified tests assert the observable result
  of the relevant failure condition. Check whether changed expectations,
  revised mocks, or removed assertions could conceal a regression.

### 4. Validate each candidate finding

Before posting a finding, answer these questions.

1. Which supported input or operating condition triggers the issue?
2. What reachable path leads from an entry point through the changed code to
   the observed or predicted outcome?
3. How does the behavior differ between the base branch and the PR under the
   same conditions?
4. Do upstream callers, validation, defaults, migrations, or retry handling
   already prevent the outcome?
5. Which user, job, dataset, or external consumer is affected?

- When a test can resolve the question, run the narrowest relevant test using
  the commands documented in CI or the repository's development guide.
  Before citing a passing test, verify that it exercises the branch in question.
- Distinguish facts established by reading the code, outcomes observed in
  tests, and assumptions about external configuration. When evidence is
  incomplete, state what was verified and what information is still needed.
- If the behavior already exists on the base branch, check whether this PR
  introduces a new trigger, expands the impact, or increases the severity.

### 5. Write actionable review comments

- Prioritize production failures, incorrect results, data loss or duplication,
  and compatibility breaks for existing consumers. Assess severity using
  the concrete impact and the conditions under which it occurs.
- Anchor each comment to the changed line that causes the issue. If the
  impact appears in an unchanged file, cite the relevant file and symbol
  as evidence.
- Explain each finding in this order: triggering condition, behavior after
  the change, and concrete impact. Include the previous behavior, supporting
  code locations, or a suggested fix when they help the author act.
- Combine symptoms that share one root cause. Tie any missing-test finding
  to the specific failure condition the test should cover.
