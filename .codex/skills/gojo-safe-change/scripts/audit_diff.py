from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from classify_change import (
    RED_PATH_PATTERNS,
    collect_changed_files,
    domains_for_file,
    git,
    is_production_file,
)


# ---------------------------------------------------------
# Added-line inspection
# ---------------------------------------------------------

SENSITIVE_ADDED_PATTERNS: dict[str, tuple[str, ...]] = {
    "DATABASE_SCHEMA": (
        "create table",
        "alter table",
        "drop table",
        "create index",
        "drop index",
    ),
    "DATABASE_WRITE": (
        "insert into",
        "update ",
        "delete from",
    ),
    "BROAD_EXCEPTION": (
        "except exception",
        "except baseexception",
    ),
    "FALLBACK": (
        "fallback",
    ),
    "BACKGROUND_EXECUTION": (
        "asyncio.create_task",
        "create_task(",
        "thread(",
        "threading.",
        "worker.start",
    ),
}


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def added_lines_for_tracked_file(base: str, path: str) -> list[str]:
    """
    Return added lines from committed/staged/unstaged diff for a tracked file.
    """
    outputs = [
        run_git("diff", "--unified=0", f"{base}...HEAD", "--", path),
        run_git("diff", "--unified=0", "--", path),
        run_git("diff", "--cached", "--unified=0", "--", path),
    ]

    added: list[str] = []

    for output in outputs:
        for line in output.splitlines():
            if not line.startswith("+"):
                continue

            # Ignore diff metadata.
            if line.startswith("+++"):
                continue

            added.append(line[1:])

    return added


def is_untracked(path: str) -> bool:
    output = run_git("ls-files", "--others", "--exclude-standard", "--", path)
    return bool(output.strip())


def added_lines(path: str, base: str) -> list[str]:
    """
    For untracked production files, inspect the whole file.
    For tracked files, inspect only added diff lines.
    """
    if is_untracked(path):
        file_path = Path(path)

        if not file_path.is_file():
            return []

        try:
            return file_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            return []

    return added_lines_for_tracked_file(base, path)


# ---------------------------------------------------------
# Audit
# ---------------------------------------------------------

def normalized_domains(path: str) -> set[str]:
    return {
        domain
        for domain in domains_for_file(path)
        if domain not in {"tests"}
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the current Gojo Git diff against an approved scope."
    )

    parser.add_argument(
        "--base",
        default="origin/main",
        help="Git base reference. Default: origin/main",
    )

    parser.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        help=(
            "Approved Change Capsule domain. "
            "Can be supplied multiple times."
        ),
    )

    args = parser.parse_args()

    repo_root = git("rev-parse", "--show-toplevel")
    branch = git("branch", "--show-current") or "(detached HEAD)"

    changed_files = collect_changed_files(args.base)

    allowed_domains = {
        value.strip().lower()
        for value in args.allow_domain
        if value.strip()
    }

    file_domains: dict[str, set[str]] = {
        path: normalized_domains(path)
        for path in changed_files
    }

    production_files = [
        path
        for path in changed_files
        if is_production_file(path)
    ]

    # -----------------------------------------------------
    # 1. Scope violations
    # -----------------------------------------------------

    scope_violations: list[tuple[str, set[str]]] = []

    if allowed_domains:
        for path in production_files:
            domains = file_domains[path]

            meaningful = {
                domain
                for domain in domains
                if domain not in {
                    "other",
                    "workflow-governance",
                }
            }

            if meaningful:
                if meaningful.isdisjoint(allowed_domains):
                    scope_violations.append((path, meaningful))
            elif "other" in domains:
                # Unknown production file is not silently accepted.
                scope_violations.append((path, domains))

    # -----------------------------------------------------
    # 2. Authority-sensitive paths
    # -----------------------------------------------------

    sensitive_paths: list[str] = []

    for path in production_files:
        lower = path.lower()

        if any(pattern in lower for pattern in RED_PATH_PATTERNS):
            sensitive_paths.append(path)

    # -----------------------------------------------------
    # 3. Added-line semantic signals
    # -----------------------------------------------------

    semantic_signals: list[tuple[str, str, str]] = []

    for path in production_files:
        for line in added_lines(path, args.base):
            lower = line.lower()

            for signal, patterns in SENSITIVE_ADDED_PATTERNS.items():
                if any(pattern in lower for pattern in patterns):
                    semantic_signals.append(
                        (
                            signal,
                            path,
                            line.strip()[:180],
                        )
                    )

    # -----------------------------------------------------
    # Status
    # -----------------------------------------------------

    status = "PASS"

    if scope_violations:
        status = "BLOCKED"

    elif sensitive_paths or semantic_signals:
        status = "NEEDS_REVIEW"

    # -----------------------------------------------------
    # Output
    # -----------------------------------------------------

    print("=" * 72)
    print("GOJO DIFF AUDIT")
    print("=" * 72)

    print(f"Repository      : {repo_root}")
    print(f"Branch          : {branch}")
    print(f"Base            : {args.base}")

    if allowed_domains:
        print(
            "Allowed domains : "
            + ", ".join(sorted(allowed_domains))
        )
    else:
        print("Allowed domains : NOT DECLARED")

    print(f"Status          : {status}")
    print()

    print(f"Changed files   : {len(changed_files)}")
    print(f"Production files: {len(production_files)}")
    print()

    # -----------------------------------------------------
    # Scope
    # -----------------------------------------------------

    print("Scope audit:")

    if not allowed_domains:
        print(
            "  - No approved domain was supplied. "
            "Scope enforcement was not performed."
        )

    elif not scope_violations:
        print("  - No out-of-scope production file detected.")

    else:
        for path, domains in scope_violations:
            print(
                f"  - BLOCKED: {path} "
                f"belongs to {', '.join(sorted(domains))}"
            )

    print()

    # -----------------------------------------------------
    # Sensitive paths
    # -----------------------------------------------------

    print("Authority-sensitive paths:")

    if not sensitive_paths:
        print("  - none")
    else:
        for path in sensitive_paths:
            print(f"  - REVIEW: {path}")

    print()

    # -----------------------------------------------------
    # Added semantic signals
    # -----------------------------------------------------

    print("Sensitive added-line signals:")

    if not semantic_signals:
        print("  - none")
    else:
        for signal, path, line in semantic_signals:
            print(f"  - [{signal}] {path}")
            print(f"      + {line}")

    print()

    # -----------------------------------------------------
    # Files
    # -----------------------------------------------------

    print("Changed files:")

    if not changed_files:
        print("  (none)")
    else:
        for path in changed_files:
            domains = ", ".join(sorted(file_domains[path]))
            kind = (
                "PRODUCTION"
                if is_production_file(path)
                else "NON-PRODUCTION"
            )

            print(
                f"  - [{kind}] "
                f"[{domains or 'other'}] "
                f"{path}"
            )

    print()
    print(
        "Interpretation:"
    )
    print(
        "  PASS         = no deterministic scope/sensitive signal found."
    )
    print(
        "  NEEDS_REVIEW = sensitive path or added behavior requires review."
    )
    print(
        "  BLOCKED      = actual production diff exceeded approved domains."
    )
    print()
    print(
        "This audit cannot lower semantic risk determined by "
        "gojo-safe-change."
    )

    print("=" * 72)

    if status == "BLOCKED":
        return 2

    if status == "NEEDS_REVIEW":
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())