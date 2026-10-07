from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def git(*args: str, check: bool = True) -> str:
    cmd = ["git", "-c", "core.quotepath=false", *args]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Git command failed:\n"
            f"{' '.join(cmd)}\n\n"
            f"{result.stderr.strip()}"
        )

    return result.stdout.strip()


def lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def normalize(path: str) -> str:
    return path.replace("\\", "/").strip()


# ---------------------------------------------------------
# Changed files
# ---------------------------------------------------------

def collect_changed_files(base: str) -> list[str]:
    changed: set[str] = set()

    # Branch commits relative to base.
    branch_diff = git("diff", "--name-only", f"{base}...HEAD", check=False)
    changed.update(normalize(x) for x in lines(branch_diff))

    # Unstaged working-tree changes.
    unstaged = git("diff", "--name-only", check=False)
    changed.update(normalize(x) for x in lines(unstaged))

    # Staged changes.
    staged = git("diff", "--cached", "--name-only", check=False)
    changed.update(normalize(x) for x in lines(staged))

    # Untracked files.
    untracked = git("ls-files", "--others", "--exclude-standard", check=False)
    changed.update(normalize(x) for x in lines(untracked))

    return sorted(x for x in changed if x)


# ---------------------------------------------------------
# File categories
# ---------------------------------------------------------

def is_test(path: str) -> bool:
    p = path.lower()
    return (
        p.startswith("tests/")
        or "/tests/" in p
        or p.endswith("_test.py")
        or p.startswith("__tests__/")
        or "/__tests__/" in p
        or p.endswith(".test.ts")
        or p.endswith(".test.tsx")
    )


def is_governance(path: str) -> bool:
    p = path.lower()
    return (
        p == "agents.md"
        or p == ".gitignore"
        or p.startswith(".codex/")
        or p.startswith("review/")
    )


def is_docs(path: str) -> bool:
    p = path.lower()
    return (
        is_governance(path)
        or p.startswith("docs/")
        or p.endswith(".md")
        or p.endswith(".txt")
    )


def is_production_file(path: str) -> bool:
    if is_test(path) or is_docs(path):
        return False

    p = path.lower()

    return not (
        p.startswith(".github/")
        or p.startswith("scripts/")
    )


# ---------------------------------------------------------
# Domain classification
# ---------------------------------------------------------

DOMAIN_PATTERNS: dict[str, tuple[str, ...]] = {
    "raw-event/evidence": (
        "raw_event",
        "raw_events",
        "db_chatlog",
        "cognitive_events",
        "provenance",
        "evidence",
    ),
    "relationship": (
        "relationship_",
        "relationship/",
        "rel_state",
        "rel_declared",
    ),
    "memory": (
        "memory_",
        "user_memory",
        "smart_recall",
        "rolling_summary",
        "episodic",
        "diary",
        "sticky",
        "recall",
    ),
    "cognitive-loop": (
        "cognitive_",
        "slow_loop",
        "fast_loop",
    ),
    "generation": (
        "generation_",
        "route_chat",
        "assistant_turn",
        "structured_output",
    ),
    "schedule/proactive": (
        "schedule",
        "phone_check",
        "proactive",
        "promise",
        "reply_availability",
        "delayed_reply",
    ),
    "identity/role": (
        "role_view",
        "subject_ref",
        "character",
        "identity",
    ),
    "audio/tts": (
        "audio",
        "tts",
        "voice",
    ),
}


def domains_for_file(path: str) -> set[str]:
    if is_governance(path):
        return {"workflow-governance"}

    p = path.lower()
    result: set[str] = set()

    for domain, patterns in DOMAIN_PATTERNS.items():
        if any(pattern in p for pattern in patterns):
            result.add(domain)

    if is_test(path):
        result.add("tests")

    if not result:
        result.add("other")

    return result


# ---------------------------------------------------------
# Architecture references
# ---------------------------------------------------------

DOMAIN_INVARIANTS: dict[str, tuple[str, ...]] = {
    "raw-event/evidence": (
        "INV-AUTH-003",
        "INV-EVID-001",
        "INV-EVID-002",
        "INV-EVID-003",
    ),
    "relationship": (
        "INV-AUTH-001",
        "INV-COG-003",
        "INV-STATE-001",
    ),
    "memory": (
        "INV-AUTH-002",
        "INV-EVID-002",
        "INV-EVID-003",
        "INV-STATE-001",
        "INV-STATE-002",
        "INV-PERSIST-001",
    ),
    "cognitive-loop": (
        "INV-COG-001",
        "INV-COG-002",
        "INV-COG-003",
    ),
    "generation": (
        "INV-GEN-001",
        "INV-EVID-001",
        "INV-FAIL-002",
    ),
    "schedule/proactive": (
        "INV-STATE-001",
        "INV-FAIL-002",
    ),
    "identity/role": (
        "INV-ID-001",
    ),
}


DOMAIN_REGRESSIONS: dict[str, tuple[str, ...]] = {
    "generation": (
        "REG-001",
        "REG-002",
    ),
    "audio/tts": (
        "REG-001",
        "REG-005 (owner may be gojo_simple)",
    ),
    "memory": (
        "REG-006",
        "REG-007",
        "REG-008",
    ),
    "raw-event/evidence": (
        "REG-006",
        "REG-008",
    ),
    "cognitive-loop": (
        "REG-007",
        "REG-008",
    ),
    "schedule/proactive": (
        "REG-009",
    ),
}


# ---------------------------------------------------------
# Risk rules
# ---------------------------------------------------------

RED_PATH_PATTERNS = (
    "/migrations/",
    "migrations/",
    "alembic/",
    "schema.sql",
    "raw_events.py",
    "memory_authority.py",
    "relationship_state.py",
    "relationship_db.py",
    "relationship_semantics.py",
    "cognitive_revision.py",
)

YELLOW_PATH_PATTERNS = (
    "worker",
    "cache",
    "route_",
    "db_",
    "utils.py",
    "config",
    ".github/workflows/",
)


def classify_risk(
    changed_files: list[str],
    file_domains: dict[str, set[str]],
) -> tuple[str, list[str]]:

    risk = "GREEN"
    reasons: list[str] = []

    production_files = [
        path for path in changed_files
        if is_production_file(path)
    ]

    production_domains: set[str] = set()
    for path in production_files:
        production_domains.update(
            d for d in file_domains[path]
            if d not in {"tests", "workflow-governance", "other"}
        )

    # RED: known authority-sensitive paths.
    red_hits = [
        path for path in production_files
        if any(pattern in path.lower() for pattern in RED_PATH_PATTERNS)
    ]

    if red_hits:
        risk = "RED"
        reasons.append(
            "Touched authority-sensitive or migration/schema path(s): "
            + ", ".join(red_hits)
        )

    # YELLOW: cross-subsystem change.
    if len(production_domains) >= 2 and risk != "RED":
        risk = "YELLOW"
        reasons.append(
            "Production change crosses multiple subsystems: "
            + ", ".join(sorted(production_domains))
        )

    # YELLOW: broad production diff.
    if len(production_files) > 5 and risk != "RED":
        risk = "YELLOW"
        reasons.append(
            f"Production diff touches {len(production_files)} files (>5)."
        )

    # YELLOW: shared/runtime-sensitive paths.
    yellow_hits = [
        path for path in production_files
        if any(pattern in path.lower() for pattern in YELLOW_PATH_PATTERNS)
    ]

    if yellow_hits and risk == "GREEN":
        risk = "YELLOW"
        reasons.append(
            "Touched shared/runtime-sensitive path(s): "
            + ", ".join(yellow_hits)
        )

    if not production_files and changed_files:
        reasons.append(
            "No production code changed; current diff is docs/tests/workflow only."
        )

    if not changed_files:
        reasons.append("No changed files detected.")

    if not reasons:
        reasons.append("Single-subsystem, low-surface-area change detected.")

    return risk, reasons


# ---------------------------------------------------------
# Report
# ---------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify Gojo change risk from the current Git diff."
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Git base reference. Default: origin/main",
    )
    args = parser.parse_args()

    repo_root = git("rev-parse", "--show-toplevel")
    branch = git("branch", "--show-current") or "(detached HEAD)"

    changed_files = collect_changed_files(args.base)

    file_domains = {
        path: domains_for_file(path)
        for path in changed_files
    }

    all_domains: set[str] = set()
    for domains in file_domains.values():
        all_domains.update(domains)

    invariants: set[str] = set()
    regressions: set[str] = set()

    for domain in all_domains:
        invariants.update(DOMAIN_INVARIANTS.get(domain, ()))
        regressions.update(DOMAIN_REGRESSIONS.get(domain, ()))

    risk, reasons = classify_risk(
        changed_files,
        file_domains,
    )

    production_count = sum(
        1 for path in changed_files if is_production_file(path)
    )

    print("=" * 68)
    print("GOJO CHANGE CLASSIFIER")
    print("=" * 68)
    print(f"Repository : {repo_root}")
    print(f"Branch     : {branch}")
    print(f"Base       : {args.base}")
    print(f"Risk       : {risk}")
    print()
    print(f"Changed files    : {len(changed_files)}")
    print(f"Production files : {production_count}")
    print()

    print("Reasons:")
    for reason in reasons:
        print(f"  - {reason}")

    print()
    print("Detected domains:")
    if all_domains:
        for domain in sorted(all_domains):
            print(f"  - {domain}")
    else:
        print("  - none")

    print()
    print("Candidate architecture invariants:")
    if invariants:
        for invariant in sorted(invariants):
            print(f"  - {invariant}")
    else:
        print("  - none detected by path heuristic")

    print()
    print("Candidate historical regressions:")
    if regressions:
        for regression in sorted(regressions):
            print(f"  - {regression}")
    else:
        print("  - none detected by path heuristic")

    print()
    print("Files:")
    if not changed_files:
        print("  (none)")
    else:
        for path in changed_files:
            domain_text = ", ".join(sorted(file_domains[path]))
            print(f"  - [{domain_text}] {path}")

    print()
    print(
        "NOTE: This is a deterministic minimum-risk classifier. "
        "Semantic review may only raise the risk level, never lower it."
    )
    print("=" * 68)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())