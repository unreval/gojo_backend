# Gojo Backend Engineering Rules

## Required workflow

For any non-trivial diagnosis, bug fix, feature, refactor, database change,
worker change, API change, memory change, cognition change, relationship change,
schedule change, or generation change:

Use the repository skill:

`.codex/skills/gojo-safe-change/SKILL.md`

Do not begin implementation before identifying:

1. the root cause;
2. the canonical authority for the affected behavior;
3. the expected change scope;
4. invariants that must remain unchanged;
5. regression tests required for the failure.

## Architectural invariants

- Do not create a second relationship authority or relationship brain.
- Do not create a second durable-memory authority.
- Generated assistant text is not authoritative cognitive evidence.
- Durable writes require valid provenance from canonical sources.
- Fast-loop authoritative judgment must not depend on an LLM.
- Slow-loop processing must not bypass the evidence pipeline.
- Slow-loop processing must not directly mutate authoritative relationship state.
- Display text must not become a canonical identity key.
- Do not introduce parallel state stores when an authoritative store already exists.

## Change discipline

Bug fixes must be minimal.

Do not perform unrelated refactors while fixing a defect.

If the proposed solution expands beyond the original subsystem,
stop and re-evaluate the impact before continuing.

Changes that affect two or more major subsystems require explicit impact review.

## Verification

A code change is not complete merely because the new behavior works.

Required verification includes, where applicable:

1. reproduction of the original failure;
2. targeted unit or contract tests;
3. regression test for the reported failure;
4. affected integration tests;
5. architecture-invariant tests;
6. review of the final Git diff.

A serious regression that is fixed must become a permanent regression test.

## Git and production safety

Unless explicitly authorized, do not:

- merge into main;
- force push;
- deploy;
- modify production databases;
- execute production backfills;
- delete branches;
- discard unrelated local changes;
- invoke paid model/API workloads purely for testing.

Do not commit unless explicitly requested.

Do not push unless explicitly requested.

## Completion

At the end of a modification, report:

- root cause;
- canonical authority involved;
- files changed;
- behavior before;
- behavior after;
- tests actually executed;
- passed and failed tests;
- architecture or API changes;
- remaining risks;
- Git branch and working-tree status.