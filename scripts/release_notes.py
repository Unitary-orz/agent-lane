#!/usr/bin/env python3
"""Generate strict GitHub Release notes from one CHANGELOG version section."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


TAG_PATTERN = re.compile(
    r"^v\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
SECOND_LEVEL_HEADING = re.compile(r"^##[ \t]+")


class ReleaseNotesError(ValueError):
    """Raised when release metadata cannot be generated safely."""


def _validated_tag(value: str, *, field: str = "tag") -> str:
    tag = value.strip()
    if not TAG_PATTERN.fullmatch(tag):
        raise ReleaseNotesError(
            f"{field} must be a SemVer tag beginning with v"
        )
    return tag


def _project_version(pyproject_path: Path) -> str:
    try:
        with pyproject_path.open("rb") as file:
            project = tomllib.load(file)["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseNotesError(
            f"could not read project version from {pyproject_path}: {exc}"
        ) from exc
    version = str(project.get("version") or "").strip()
    if not version:
        raise ReleaseNotesError(
            f"project version is empty in {pyproject_path}"
        )
    return version


def extract_version_section(changelog: str, tag: str) -> str:
    heading = re.compile(
        rf"^##[ \t]+\[{re.escape(tag)}\]"
        rf"[ \t]+-[ \t]+\d{{4}}-\d{{2}}-\d{{2}}"
        rf"(?:[ \t]+.*)?[ \t]*$"
    )
    lines = changelog.splitlines()
    matches = [index for index, line in enumerate(lines) if heading.fullmatch(line)]
    if not matches:
        raise ReleaseNotesError(
            f"CHANGELOG does not contain an exact section for {tag}"
        )
    if len(matches) > 1:
        raise ReleaseNotesError(
            f"CHANGELOG contains more than one section for {tag}"
        )

    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if SECOND_LEVEL_HEADING.match(lines[index]):
            end = index
            break
    body = "\n".join(lines[start + 1 : end]).strip()
    if not body:
        raise ReleaseNotesError(f"CHANGELOG section for {tag} is empty")
    return body


def _release_highlights(section: str) -> str:
    return "\n".join(
        f"#{line}" if line.startswith("### ") else line
        for line in section.splitlines()
    )


def render_release_notes(
    *,
    tag: str,
    section: str,
    repository_url: str | None = None,
    commit_sha: str | None = None,
    previous_tag: str | None = None,
) -> str:
    repo_url = (repository_url or "").strip().rstrip("/")
    sha = (commit_sha or "").strip()
    previous = (
        _validated_tag(previous_tag, field="previous tag")
        if previous_tag
        else None
    )

    lines = [
        f"## agent-lane {tag}",
        "",
        "### Overview",
        "",
        f"- Release tag: `{tag}`",
    ]
    if sha:
        short_sha = sha[:7]
        commit = (
            f"[`{short_sha}`]({repo_url}/commit/{sha})"
            if repo_url
            else f"`{short_sha}`"
        )
        lines.append(f"- Commit: {commit}")

    lines.extend(
        [
            "",
            "### Highlights",
            "",
            _release_highlights(section),
            "",
            "### Packages",
            "",
            "- Python wheel (`.whl`)",
            "- Source distribution (`.tar.gz`)",
            "",
            "### Verification",
            "",
            "Install the wheel in a clean environment and run:",
            "",
            "```bash",
            "agent-lane --version",
            "```",
        ]
    )
    if repo_url:
        reference = sha or tag
        lines.extend(
            [
                "",
                "### References",
                "",
                f"- [Changelog]({repo_url}/blob/{reference}/CHANGELOG.md)",
            ]
        )
        if previous:
            lines.append(
                f"- [Compare {previous} → {tag}]"
                f"({repo_url}/compare/{previous}...{tag})"
            )
    return "\n".join(lines).rstrip() + "\n"


def generate_release_notes(
    *,
    tag: str,
    changelog_path: Path,
    pyproject_path: Path,
    repository_url: str | None = None,
    commit_sha: str | None = None,
    previous_tag: str | None = None,
) -> str:
    release_tag = _validated_tag(tag)
    expected_version = release_tag[1:]
    project_version = _project_version(pyproject_path)
    if project_version != expected_version:
        raise ReleaseNotesError(
            "release metadata mismatch: "
            f"tag {release_tag} expects {expected_version}, "
            f"but {pyproject_path} declares {project_version}"
        )
    try:
        changelog = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseNotesError(
            f"could not read CHANGELOG from {changelog_path}: {exc}"
        ) from exc
    section = extract_version_section(changelog, release_tag)
    return render_release_notes(
        tag=release_tag,
        section=section,
        repository_url=repository_url,
        commit_sha=commit_sha,
        previous_tag=previous_tag,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate GitHub Release notes from CHANGELOG.md"
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--repository-url")
    parser.add_argument("--commit-sha")
    parser.add_argument("--previous-tag")
    parser.add_argument("--output", type=Path, help="write to a file instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        notes = generate_release_notes(
            tag=args.tag,
            changelog_path=args.changelog,
            pyproject_path=args.pyproject,
            repository_url=args.repository_url,
            commit_sha=args.commit_sha,
            previous_tag=args.previous_tag,
        )
        if args.output is None:
            sys.stdout.write(notes)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(notes, encoding="utf-8")
    except (OSError, ReleaseNotesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
