import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_notes.py"


def _write_project(tmp_path: Path, *, version: str, changelog: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "agent-lane"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--changelog",
            str(tmp_path / "CHANGELOG.md"),
            "--pyproject",
            str(tmp_path / "pyproject.toml"),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_notes_extract_exact_tag_and_preserve_styled_content(tmp_path):
    _write_project(
        tmp_path,
        version="1.0.0-rc.2",
        changelog="""# Changelog

## [Unreleased] 🚧

## [v1.0.0-rc.2] - 2026-08-09 🚀

> Safer release.

### ✨ Added

- Added strict release notes.

### 🚀 Engineering

- Added tag-driven publishing.

## [v1.0.0-rc.1] - 2026-08-08 🌱

- Older content.
""",
    )

    result = _run(
        tmp_path,
        "--tag",
        "v1.0.0-rc.2",
        "--repository-url",
        "https://example.com/owner/repo",
        "--commit-sha",
        "abcdef1234567890",
        "--previous-tag",
        "v1.0.0-rc.1",
    )

    assert result.returncode == 0, result.stderr
    assert "## agent-lane v1.0.0-rc.2" in result.stdout
    assert "> Safer release." in result.stdout
    assert "#### ✨ Added" in result.stdout
    assert "#### 🚀 Engineering" in result.stdout
    assert "Older content" not in result.stdout
    assert "[`abcdef1`]" in result.stdout
    assert "Compare v1.0.0-rc.1 → v1.0.0-rc.2" in result.stdout


def test_release_notes_write_output_file(tmp_path):
    _write_project(
        tmp_path,
        version="1.2.3",
        changelog="""# Changelog

## [v1.2.3] - 2026-08-09

### 🐞 Fixed

- Fixed one issue.
""",
    )
    output = tmp_path / "out" / "release-notes.md"

    result = _run(
        tmp_path,
        "--tag",
        "v1.2.3",
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Fixed one issue" in output.read_text(encoding="utf-8")


def test_release_notes_fail_when_project_version_differs(tmp_path):
    _write_project(
        tmp_path,
        version="1.0.0-rc.1",
        changelog="""## [v1.0.0-rc.2] - 2026-08-09

- Release content.
""",
    )

    result = _run(tmp_path, "--tag", "v1.0.0-rc.2")

    assert result.returncode == 1
    assert "release metadata mismatch" in result.stderr


def test_release_notes_fail_when_changelog_section_is_missing(tmp_path):
    _write_project(
        tmp_path,
        version="1.0.0-rc.2",
        changelog="""## [v1.0.0-rc.1] - 2026-08-08

- Older content.
""",
    )

    result = _run(tmp_path, "--tag", "v1.0.0-rc.2")

    assert result.returncode == 1
    assert "exact section" in result.stderr


def test_release_notes_fail_when_version_heading_omits_date(tmp_path):
    _write_project(
        tmp_path,
        version="1.0.0-rc.2",
        changelog="""## [v1.0.0-rc.2] 🚀

- Release content.
""",
    )

    result = _run(tmp_path, "--tag", "v1.0.0-rc.2")

    assert result.returncode == 1
    assert "exact section" in result.stderr


def test_release_notes_fail_when_changelog_section_is_duplicated(tmp_path):
    _write_project(
        tmp_path,
        version="1.0.0-rc.2",
        changelog="""## [v1.0.0-rc.2] - 2026-08-09

- First copy.

## [v1.0.0-rc.2] - 2026-08-09

- Second copy.
""",
    )

    result = _run(tmp_path, "--tag", "v1.0.0-rc.2")

    assert result.returncode == 1
    assert "more than one section" in result.stderr
