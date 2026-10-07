from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from classify_change import (
    DOMAIN_INVARIANTS,
    DOMAIN_REGRESSIONS,
    RED_PATH_PATTERNS,
    classify_risk,
    collect_changed_files,
    domains_for_file,
    git,
    is_production_file,
)


def md_list(items: list[str], empty: str = "None") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- `{item}`" for item in items)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a concise Gojo change review report."
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
        help="Approved Change Capsule domain. Can be repeated.",
    )

    parser.add_argument(
        "--output",
        default="review/latest.md",
        help="Output report path. Default: review/latest.md",
    )

    args = parser.parse_args()

    repo_root = git("rev-parse", "--show-toplevel")
    branch = git("branch", "--show-current") or "(detached HEAD)"
    head = git("rev-parse", "HEAD")

    changed_files = collect_changed_files(args.base)

    # Avoid the generated report auditing itself.
    output_normalized = args.output.replace("\\", "/")
    changed_files = [
        path for path in changed_files
        if path.replace("\\", "/") != output_normalized
    ]

    file_domains = {
        path: domains_for_file(path)
        for path in changed_files
    }

    production_files = [
        path for path in changed_files
        if is_production_file(path)
    ]

    non_production_files = [
        path for path in changed_files
        if not is_production_file(path)
    ]

    all_domains: set[str] = set()

    for domains in file_domains.values():
        all_domains.update(domains)

    invariants: set[str] = set()
    regressions: set[str] = set()

    for domain in all_domains:
        invariants.update(DOMAIN_INVARIANTS.get(domain, ()))
        regressions.update(DOMAIN_REGRESSIONS.get(domain, ()))

    risk, risk_reasons = classify_risk(
        changed_files,
        file_domains,
    )

    allowed_domains = {
        value.strip().lower()
        for value in args.allow_domain
        if value.strip()
    }

    # -----------------------------------------------------
    # Scope audit
    # -----------------------------------------------------

    scope_violations: list[str] = []

    if allowed_domains:
        for path in production_files:
            domains = {
                domain
                for domain in file_domains[path]
                if domain not in {
                    "tests",
                    "workflow-governance",
                }
            }

            meaningful = {
                domain
                for domain in domains
                if domain != "other"
            }

            if meaningful:
                if meaningful.isdisjoint(allowed_domains):
                    scope_violations.append(
                        f"{path} -> {', '.join(sorted(meaningful))}"
                    )
            elif "other" in domains:
                scope_violations.append(
                    f"{path} -> unclassified production file"
                )

    sensitive_paths = [
        path
        for path in production_files
        if any(
            pattern in path.lower()
            for pattern in RED_PATH_PATTERNS
        )
    ]

    # -----------------------------------------------------
    # Overall status
    # -----------------------------------------------------

    if scope_violations:
        status = "BLOCKED"
    elif not allowed_domains:
        status = "NEEDS REVIEW"
    elif risk in {"YELLOW", "RED"} or sensitive_paths:
        status = "NEEDS REVIEW"
    else:
        status = "SAFE"

    # -----------------------------------------------------
    # Git state
    # -----------------------------------------------------

    git_status = git("status", "--short", check=False)

    status_lines = [
        line
        for line in git_status.splitlines()
        if line.strip()
    ]

    generated_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    # -----------------------------------------------------
    # Human-readable report
    # -----------------------------------------------------

    report: list[str] = []

    report.append("# Gojo Change Review")
    report.append("")
    report.append(
        f"_Generated automatically: {generated_at}_"
    )
    report.append("")

    report.append("## Status")
    report.append("")
    report.append(f"**{status}**")
    report.append("")

    report.append("## Risk")
    report.append("")
    report.append(f"**{risk}**")
    report.append("")

    for reason in risk_reasons:
        report.append(f"- {reason}")

    report.append("")

    report.append("## Git Baseline")
    report.append("")
    report.append(f"- Repository: `{repo_root}`")
    report.append(f"- Branch: `{branch}`")
    report.append(f"- HEAD: `{head}`")
    report.append(f"- Base: `{args.base}`")
    report.append("")

    report.append("## Approved Scope")
    report.append("")

    if allowed_domains:
        for domain in sorted(allowed_domains):
            report.append(f"- `{domain}`")
    else:
        report.append("- **NOT DECLARED**")

    report.append("")

    report.append("## Detected Domains")
    report.append("")

    if all_domains:
        for domain in sorted(all_domains):
            report.append(f"- `{domain}`")
    else:
        report.append("- None")

    report.append("")

    report.append("## Files Changed")
    report.append("")
    report.append(
        f"- Production: **{len(production_files)}**"
    )
    report.append(
        f"- Non-production: **{len(non_production_files)}**"
    )
    report.append("")

    report.append("### Production")
    report.append("")
    report.append(md_list(production_files))
    report.append("")

    report.append("### Tests / Docs / Workflow")
    report.append("")
    report.append(md_list(non_production_files))
    report.append("")

    report.append("## Scope Audit")
    report.append("")

    if not allowed_domains:
        report.append(
            "- ⚠ No approved domain supplied; "
            "scope enforcement is incomplete."
        )
    elif scope_violations:
        for violation in scope_violations:
            report.append(
                f"- ❌ OUT OF SCOPE: `{violation}`"
            )
    else:
        report.append(
            "- ✅ No out-of-scope production file detected."
        )

    report.append("")

    report.append("## Authority-sensitive Paths")
    report.append("")

    if sensitive_paths:
        for path in sensitive_paths:
            report.append(f"- ⚠ `{path}`")
    else:
        report.append("- None detected")

    report.append("")

    report.append("## Candidate Architecture Invariants")
    report.append("")

    if invariants:
        for invariant in sorted(invariants):
            report.append(f"- `{invariant}`")
    else:
        report.append(
            "- None detected by deterministic path heuristic"
        )

    report.append("")

    report.append("## Candidate Historical Regressions")
    report.append("")

    if regressions:
        for regression in sorted(regressions):
            report.append(f"- `{regression}`")
    else:
        report.append(
            "- None detected by deterministic path heuristic"
        )

    report.append("")

    report.append("## Tests")
    report.append("")
    report.append(
        "**UNVERIFIED — this report generator does not run tests.**"
    )
    report.append("")
    report.append(
        "Actual executed test commands and results must be added "
        "by the gojo-safe-change completion workflow."
    )
    report.append("")

    report.append("## Architecture Impact")
    report.append("")
    report.append(
        "This section intentionally does not claim that an authority "
        "is unchanged merely because a path heuristic did not detect it."
    )
    report.append("")
    report.append(
        "Semantic architecture impact must be confirmed during "
        "Change Capsule and Diff Audit."
    )
    report.append("")

    report.append("## Current Git Status")
    report.append("")

    if status_lines:
        report.append("```text")
        report.extend(status_lines)
        report.append("```")
    else:
        report.append("Working tree clean.")

    report.append("")

    report.append("## Automatic Review Boundary")
    report.append("")
    report.append(
        "This report is deterministic evidence about the Git diff."
    )
    report.append("")
    report.append(
        "It cannot lower a GREEN/YELLOW/RED risk assigned by "
        "semantic gojo-safe-change review."
    )
    report.append("")
    report.append(
        "A semantic review may always raise the risk level."
    )
    report.append("")

    output_path = Path(args.output)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print("=" * 68)
    print("GOJO REVIEW REPORT")
    print("=" * 68)
    print(f"Status : {status}")
    print(f"Risk   : {risk}")
    print(f"Output : {output_path.as_posix()}")
    print(
        f"Files  : {len(production_files)} production / "
        f"{len(non_production_files)} non-production"
    )

    if scope_violations:
        print(
            f"Scope  : {len(scope_violations)} violation(s)"
        )
    else:
        print("Scope  : no deterministic violation detected")

    print("=" * 68)

    return 2 if status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())