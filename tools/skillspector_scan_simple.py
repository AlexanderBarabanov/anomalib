#!/usr/bin/env python3
"""SkillSpector batch scanner for anomalib skills.

Discovers skill directories, runs SkillSpector scans, generates Markdown report,
and exits with status code based on threshold.

Usage:
    skillspector_scan_simple.py --all
    skillspector_scan_simple.py --changed --base-ref origin/main
    skillspector_scan_simple.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SKILLS_ROOT = Path(".agents/skills")
MANIFEST_NAMES = {"skill.md"}
DEFAULT_THRESHOLD = 50
MAX_COMMENT_CHARS = 60000


def _has_manifest(directory: Path) -> bool:
    """Return True if directory directly contains a SKILL.md (any case)."""
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.lower() in MANIFEST_NAMES:
                return True
    except OSError:
        return False
    return False


def discover_all_skills(root: Path = SKILLS_ROOT) -> list[Path]:
    """Find every skill directory (one that directly contains a SKILL.md)."""
    if not root.is_dir():
        return []
    skills: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if any(name.lower() in MANIFEST_NAMES for name in filenames):
            skills.append(Path(dirpath))
    return sorted(set(skills))


def _changed_files(base_ref: str) -> list[Path]:
    """Return repo-relative paths changed vs base_ref (three-dot diff)."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        # Fall back to two-dot if the merge base is unavailable (shallow clone).
        out = subprocess.run(
            ["git", "diff", "--name-only", base_ref],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    return [Path(line.strip()) for line in out.splitlines() if line.strip()]


def discover_changed_skills(base_ref: str, root: Path = SKILLS_ROOT) -> list[Path]:
    """Map changed files under the skills tree to their owning skill dirs."""
    changed = _changed_files(base_ref)
    skills: set[Path] = set()
    root_resolved = root.resolve()
    for rel in changed:
        try:
            rel.resolve().relative_to(root_resolved)
        except ValueError:
            if root not in rel.parents and rel != root:
                continue
        # Walk up from the file's directory until we find the skill dir.
        current = (rel if rel.is_dir() else rel.parent)
        while True:
            if current == root or current == Path("."):
                break
            if current.is_dir() and _has_manifest(current):
                skills.add(current)
                break
            # Stop once we climb above the skills root.
            if root not in current.parents:
                break
            current = current.parent
    return sorted(skills)


def scan_skill(directory: Path) -> dict:
    """Run skillspector on one skill dir; return a normalized result dict."""
    result: dict = {"path": str(directory), "status": "ok"}
    try:
        proc = subprocess.run(
            [
                "skillspector",
                "scan",
                str(directory),
                "--no-llm",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        result["status"] = "error"
        result["error"] = "skillspector CLI not found on PATH"
        return result

    # Exit code 2 == real error. 0/1 are both valid (1 == high score).
    if proc.returncode == 2:
        result["status"] = "error"
        result["error"] = (proc.stderr or proc.stdout or "scan failed").strip()[:500]
        return result

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result["status"] = "error"
        result["error"] = "could not parse skillspector JSON output"
        result["raw"] = (proc.stdout or proc.stderr or "")[:500]
        return result

    risk = data.get("risk_assessment", {})
    result["name"] = (data.get("skill") or {}).get("name") or directory.name
    result["score"] = int(risk.get("score") or 0)
    result["severity"] = (risk.get("severity") or "LOW").upper()
    result["recommendation"] = risk.get("recommendation") or "SAFE"
    result["issues"] = data.get("issues") or []
    return result


def _severity_emoji(severity: str) -> str:
    return {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "🔴"}.get(severity, "")


def build_markdown(results: list[dict], threshold: int) -> str:
    """Produce the Markdown summary."""
    scanned = [r for r in results if r.get("status") == "ok"]
    errors = [r for r in results if r.get("status") == "error"]
    flagged = sorted(
        [r for r in scanned if r["score"] > threshold],
        key=lambda r: r["score"],
        reverse=True,
    )
    high_critical = len(flagged)

    sev_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    for r in scanned:
        sev_counts[r["severity"]] = sev_counts.get(r["severity"], 0) + 1

    status = "❌ ISSUES FOUND" if high_critical else "✅ PASSED"
    emoji = "⚠️" if high_critical else "🎉"

    lines = []
    lines.append(f"## {emoji} SkillSpector Security Scan")
    lines.append("")
    lines.append(
        "Static analysis (`--no-llm`) by "
        "[SkillSpector](https://github.com/NVIDIA/skillspector) (NVIDIA, Apache-2.0)."
    )
    lines.append("")
    lines.append(f"**Status:** {status}")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Skills scanned | {len(scanned)} |")
    lines.append(f"| 🔴 HIGH/CRITICAL (score > {threshold}) | {high_critical} |")
    lines.append(f"| 🟡 MEDIUM | {sev_counts['MEDIUM']} |")
    lines.append(f"| 🟢 LOW / clean | {sev_counts['LOW']} |")
    if errors:
        lines.append(f"| ⚠️ Scan errors | {len(errors)} |")
    lines.append("")

    if flagged:
        lines.append("### 🔴 Flagged skills")
        lines.append("")
        lines.append("| Skill | Score | Severity | Recommendation | Issues | Top findings |")
        lines.append("|-------|-------|----------|----------------|--------|--------------|")
        for r in flagged[:30]:
            top_rules = ", ".join(
                sorted({str(i.get("rule_id") or "?") for i in r.get("issues", [])})[:5]
            )
            rec = str(r.get("recommendation", "")).replace("_", " ")
            lines.append(
                f"| `{r['name']}` | {r['score']}/100 | "
                f"{_severity_emoji(r['severity'])} {r['severity']} | {rec} | "
                f"{len(r.get('issues', []))} | {top_rules} |"
            )
        if len(flagged) > 30:
            lines.append("")
            lines.append(f"_...and {len(flagged) - 30} more flagged skill(s)._")
        lines.append("")

    if errors:
        lines.append("### ⚠️ Scan errors")
        lines.append("")
        for r in errors[:10]:
            lines.append(f"- `{r['path']}` — {r.get('error', 'unknown error')}")
        if len(errors) > 10:
            lines.append(f"- _...and {len(errors) - 10} more._")
        lines.append("")

    if not flagged and not errors:
        lines.append("No HIGH/CRITICAL findings. ✅")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Generated by SkillSpector. Score bands: 0-20 LOW, 21-50 MEDIUM, "
        "51-80 HIGH, 81-100 CRITICAL._"
    )

    text = "\n".join(lines)
    if len(text) > MAX_COMMENT_CHARS:
        text = text[: MAX_COMMENT_CHARS - 200] + "\n\n_...report truncated (too long)._"
    return text


def _write_output(name: str, value: str) -> None:
    """Append a key=value pair to $GITHUB_OUTPUT (no-op if unset)."""
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<__EOF__\n{value}\n__EOF__\n")
        else:
            fh.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="SkillSpector batch scanner.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Scan every skill dir.")
    scope.add_argument(
        "--changed", action="store_true", help="Scan only skills changed vs --base-ref."
    )
    parser.add_argument("--base-ref", default="origin/main", help="Base ref for --changed.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        help="Set output failed=true (and exit 1) if any skill exceeds threshold.",
    )
    parser.add_argument("--output-md", default="skillspector-report.md")
    parser.add_argument(
        "--dry-run", action="store_true", help="List discovered skills without scanning."
    )
    args = parser.parse_args()

    if args.changed:
        skills = discover_changed_skills(args.base_ref)
    else:
        skills = discover_all_skills()

    print(f"Discovered {len(skills)} skill director{'y' if len(skills) == 1 else 'ies'}.")

    if args.dry_run:
        for s in skills:
            print(f"  {s}")
        return 0

    if not skills:
        # Nothing to scan is a pass.
        Path(args.output_md).write_text(
            "## 🎉 SkillSpector Security Scan\n\n"
            "No skill components were changed in this PR.\n",
            encoding="utf-8",
        )
        _write_output("scanned", "0")
        _write_output("high_critical_count", "0")
        _write_output("errors", "0")
        _write_output("failed", "false")
        return 0

    results: list[dict] = []
    for idx, skill in enumerate(skills, 1):
        print(f"[{idx}/{len(skills)}] scanning {skill}")
        results.append(scan_skill(skill))

    scanned = [r for r in results if r.get("status") == "ok"]
    errors = [r for r in results if r.get("status") == "error"]
    flagged = [r for r in scanned if r["score"] > args.threshold]

    markdown = build_markdown(results, args.threshold)
    Path(args.output_md).write_text(markdown, encoding="utf-8")

    print(
        f"Scanned={len(scanned)} flagged={len(flagged)} errors={len(errors)} "
        f"(threshold={args.threshold})"
    )

    failed = bool(flagged) and args.fail_on_risk
    _write_output("scanned", str(len(scanned)))
    _write_output("high_critical_count", str(len(flagged)))
    _write_output("errors", str(len(errors)))
    _write_output("failed", "true" if failed else "false")

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
