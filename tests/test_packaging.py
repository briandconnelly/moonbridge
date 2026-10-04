"""Guards on the plugin packaging: JSON validity and cross-file version/tool parity."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from moonbridge import __version__, server

ROOT = Path(__file__).resolve().parents[1]


def test_project_identity_is_moonbridge():
    """All distributable identities use the Moonbridge name."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "moonbridge"
    assert pyproject["project"]["scripts"] == {"moonbridge-mcp": "moonbridge.server:main"}

    for relpath in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        assert _load_json(relpath)["name"] == "moonbridge"

    assert set(_load_json(".mcp.json")["mcpServers"]) == {"moonbridge"}


def test_legacy_project_identity_is_absent_from_current_text():
    """The pre-release rename is a clean break, without compatibility aliases."""
    legacy_identities = (
        "kimi" + "-in-claude",
        "kimi" + "_in_claude",
        "KIMI" + "_IN_CLAUDE",
    )
    text_suffixes = {".json", ".lock", ".md", ".py", ".toml", ".yaml", ".yml"}
    ignored_parts = {
        ".claude",  # harness-local settings, not distributed repository content
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }

    stale: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in text_suffixes:
            continue
        if ignored_parts.intersection(path.relative_to(ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8")
        if any(identity in text for identity in legacy_identities):
            stale.append(str(path.relative_to(ROOT)))

    assert stale == []


def _load_json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def _declared_py_minors() -> set[str]:
    """Python minor versions advertised by the trove classifiers in pyproject.toml."""
    classifiers = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["classifiers"]
    return {
        m.group(1)
        for c in classifiers
        if (m := re.fullmatch(r"Programming Language :: Python :: (\d+\.\d+)", c))
    }


def _parse_py_matrix(workflow: str) -> set[str]:
    """Python minors from a workflow's `python-version` matrix.

    Handles both the inline-list form (`python-version: ["3.11", "3.12"]`) and the
    block-list form (`python-version:` followed by indented `- "3.11"` items), so a
    harmless YAML reformat doesn't false-fail the drift guard."""
    inline = re.search(r"python-version:\s*\[([^\]]*)\]", workflow)
    if inline:
        return set(re.findall(r"\d+\.\d+", inline.group(1)))
    # Block-list form: capture the contiguous run of `- <version>` items that
    # follows the key, stopping at the first line that isn't a list item.
    block = re.search(
        r"python-version:[ \t]*\n((?:[ \t]*-[ \t]*['\"]?\d+\.\d+['\"]?[ \t]*\n)+)", workflow
    )
    assert block, "could not find a python-version matrix in the workflow"
    return set(re.findall(r"\d+\.\d+", block.group(1)))


def _test_matrix_minors() -> set[str]:
    """Python minor versions exercised by the reusable test workflow in test.yml."""
    return _parse_py_matrix((ROOT / ".github/workflows/test.yml").read_text())


def test_python_support_matrix_matches_classifiers():
    """The advertised support set and the CI matrix can't silently diverge (issue #17)."""
    declared = _declared_py_minors()
    assert declared, "no Python minor classifiers found"
    assert declared == _test_matrix_minors()


def test_matrix_parser_handles_inline_and_block_yaml():
    """The drift guard tolerates either YAML list style for python-version."""
    inline = '      python-version: ["3.11", "3.12", "3.13"]\n'
    block = '      python-version:\n        - "3.11"\n        - "3.12"\n        - "3.13"\n'
    expected = {"3.11", "3.12", "3.13"}
    assert _parse_py_matrix(inline) == expected
    assert _parse_py_matrix(block) == expected


def test_requires_python_floor_is_lowest_declared():
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    floor = re.search(r">=\s*(\d+\.\d+)", requires)
    assert floor, f"could not parse a >= floor from requires-python: {requires!r}"
    lowest = min(_declared_py_minors(), key=lambda v: tuple(map(int, v.split("."))))
    assert floor.group(1) == lowest


def test_operating_system_classifiers_declare_posix_only():
    """The async-job safety layer is POSIX-only, so the trove classifiers must not
    advertise `OS Independent` and must name the supported POSIX platforms (#232)."""
    classifiers = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["classifiers"]
    os_classifiers = {c for c in classifiers if c.startswith("Operating System ::")}
    assert "Operating System :: OS Independent" not in os_classifiers
    assert "Operating System :: MacOS" in os_classifiers
    assert "Operating System :: POSIX :: Linux" in os_classifiers


def test_plugin_manifest_valid_and_versioned():
    manifest = _load_json(".claude-plugin/plugin.json")
    assert manifest["name"] == "moonbridge"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"]


def test_codex_plugin_manifest_valid_versioned_and_reuses_portable_components():
    """Codex gets its own manifest without duplicating the skill or MCP config."""
    codex = _load_json(".codex-plugin/plugin.json")
    claude = _load_json(".claude-plugin/plugin.json")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert codex["name"] == "moonbridge"
    assert codex["version"] == pyproject["project"]["version"]
    assert codex["skills"] == claude["skills"] == "./skills/"
    assert codex["mcpServers"] == claude["mcpServers"] == "./.mcp.json"
    # Claude slash-command files are not a Codex plugin component. The shared skill
    # provides Codex-native routing to the same MCP tools instead.
    assert "commands" not in codex


def test_pyproject_version_matches_package():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    # __version__ resolves from installed metadata; tolerate dev/unknown in source trees.
    if not __version__.endswith("+unknown"):
        assert __version__ == pyproject["project"]["version"]


def _skill_frontmatter(skill_md: Path) -> dict[str, object]:
    """Parse and validate one shipped skill's YAML frontmatter."""
    lines = skill_md.read_text().splitlines()
    assert lines and lines[0] == "---", f"{skill_md} frontmatter must start on the first line"
    # Later "---" lines are markdown horizontal rules, not fences; only the first close counts.
    closing_fence = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    assert closing_fence is not None, f"{skill_md} frontmatter is never closed"
    assert closing_fence > 1, f"{skill_md} has empty frontmatter"

    frontmatter_lines = lines[1:closing_fence]
    parsed = yaml.safe_load("\n".join(frontmatter_lines))
    assert isinstance(parsed, dict), f"{skill_md} frontmatter must be a YAML mapping"
    for key in ("name", "description"):
        assert isinstance(parsed.get(key), str), f"{skill_md} {key} must be a string"
    assert parsed["name"] == skill_md.parent.name, f"{skill_md} frontmatter name mismatch"
    assert len(parsed["description"]) <= 650, f"{skill_md} description exceeds 650 characters"
    return parsed


def test_skills_present_with_frontmatter():
    """Every skills/<dir>/SKILL.md has valid, bounded YAML frontmatter."""
    skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert skill_files, "no skills found under skills/*/SKILL.md"
    for skill_md in skill_files:
        _skill_frontmatter(skill_md)
    # One router skill owns ordinary calls and composed deliberation workflows.
    names = {p.parent.name for p in skill_files}
    assert "collaborating-with-kimi" in names
    assert "deliberating-with-kimi" not in names
    reference_dir = ROOT / "skills/collaborating-with-kimi/references"
    references = {p.name for p in reference_dir.glob("*.md")}
    assert {"independent-attempt.md", "review-revise.md"} <= references


def test_commands_have_valid_frontmatter():
    """Every commands/**/*.md has a YAML-mapping frontmatter with a string description. An
    unquoted description containing ": " (e.g. "(Deprecated: use amicus) ...") is not valid
    YAML, so a command loader cannot read the file (Copilot, #22)."""
    command_files = sorted((ROOT / "commands").rglob("*.md"))
    assert command_files, "no slash commands found under commands/"
    for command_md in command_files:
        lines = command_md.read_text().splitlines()
        assert lines and lines[0] == "---", f"{command_md} frontmatter must start on line one"
        closing_fence = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), None)
        assert closing_fence is not None, f"{command_md} frontmatter is never closed"
        parsed = yaml.safe_load("\n".join(lines[1:closing_fence]))
        assert isinstance(parsed, dict), f"{command_md} frontmatter must be a YAML mapping"
        assert isinstance(parsed.get("description"), str), f"{command_md} needs a description"


def _recorded_treatment_passes(scenarios_text: str) -> set[str]:
    """Scenario ids with a passing treatment row in the '## Run record' table.

    Only rows after the '## Run record' heading count. A row passes when its Run cell starts with
    'treatment' and its Passed cell is exactly 'pass', optionally followed by a parenthesized
    qualifier (e.g. 'pass (A-F)'). Escaped pipes (\\|) inside a cell do not split it.
    """
    heading = re.search(r"(?m)^## Run record\s*$", scenarios_text)
    if heading is None:
        return set()
    recorded = set()
    for line in scenarios_text[heading.end() :].splitlines():
        masked = line.replace("\\|", "\x00")
        cells = [cell.replace("\x00", "|").strip() for cell in masked.strip().strip("|").split("|")]
        # Row: | Date | Scenario | Run | Model | Harness/version | Passed | Evidence/artifact |
        if (
            len(cells) == 7
            and re.fullmatch(r"S\d+", cells[1])
            and cells[2].startswith("treatment")
            and re.fullmatch(r"pass(\s*\(.*\))?", cells[5])
        ):
            recorded.add(cells[1])
    return recorded


ROW = "| 2026-07-12 | {sid} | {run} | m | h | {passed} | {evidence} |"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Counted: plain pass and parenthesized-qualifier pass.
        (ROW.format(sid="S1", run="treatment", passed="pass", evidence="e"), {"S1"}),
        (ROW.format(sid="S1", run="treatment", passed="pass (A-F)", evidence="e"), {"S1"}),
        # Counted: an escaped pipe inside the evidence cell does not split the row.
        (ROW.format(sid="S1", run="treatment", passed="pass", evidence=r"a \| b"), {"S1"}),
        # Not counted: baseline runs, non-pass results, and 'pass'-prefixed non-pass values.
        (ROW.format(sid="S1", run="baseline (abc)", passed="pass", evidence="e"), set()),
        (ROW.format(sid="S1", run="treatment", passed="fail", evidence="e"), set()),
        (ROW.format(sid="S1", run="treatment", passed="pass pending", evidence="e"), set()),
    ],
)
def test_recorded_treatment_passes_row_forms(body, expected):
    text = f"## S1: Example\n\n## Run record\n\n{body}\n"
    assert _recorded_treatment_passes(text) == expected


def test_recorded_treatment_passes_ignores_rows_outside_run_record():
    row = ROW.format(sid="S1", run="treatment", passed="pass", evidence="e")
    text = f"## S1: Example\n\n{row}\n\n## Run record\n\n(no rows yet)\n"
    assert _recorded_treatment_passes(text) == set()


def test_recorded_treatment_passes_finds_heading_at_start_of_file():
    row = ROW.format(sid="S1", run="treatment", passed="pass", evidence="e")
    assert _recorded_treatment_passes(f"## Run record\n\n{row}\n") == {"S1"}


def test_skill_scenarios_have_recorded_treatment_runs():
    """Every behavioral scenario has a checked-in passing treatment run in the run record.

    A scenario landing without a run artifact is aspirational, not protective — the current skill
    text was never exercised against it. Run the scenario per the harness protocol in scenarios.md
    and append the row before landing the scenario (or a skill-text change that invalidates it).
    """
    text = (ROOT / "skills/collaborating-with-kimi/tests/scenarios.md").read_text()
    scenario_ids = set(re.findall(r"(?m)^## (S\d+):", text))
    assert scenario_ids, "no scenarios found in scenarios.md"
    missing = scenario_ids - _recorded_treatment_passes(text)
    assert not missing, f"scenarios without a recorded passing treatment run: {sorted(missing)}"


@pytest.mark.parametrize(
    "text",
    [
        "name: example\ndescription: example\n",
        "---\nname: example\ndescription: example\n",
        "body\n---\nname: example\ndescription: example\n---\n",
    ],
)
def test_skill_frontmatter_rejects_missing_or_misplaced_fences(tmp_path, text):
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(text)
    with pytest.raises(AssertionError):
        _skill_frontmatter(skill_md)


@pytest.mark.parametrize(
    "text",
    [
        # A horizontal rule in the body is markdown, not a frontmatter fence.
        "---\nname: example\ndescription: example\n---\nbody\n---\nmore body\n",
        # Plain-scalar descriptions are valid frontmatter; folded style is not a contract.
        "---\nname: example\ndescription: example\n---\nbody\n",
    ],
)
def test_skill_frontmatter_accepts_body_rules_and_plain_descriptions(tmp_path, text):
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(text)
    assert _skill_frontmatter(skill_md)["name"] == "example"


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("- name\n- description", "mapping"),
        ("name: example\n", "description must be a string"),
        ("name: 123\ndescription: >-\n  example", "name must be a string"),
        ("name: wrong\ndescription: >-\n  example", "name mismatch"),
        (f"name: example\ndescription: >-\n  {'x' * 651}", "exceeds 650"),
    ],
)
def test_skill_frontmatter_rejects_invalid_yaml_contract(tmp_path, frontmatter, message):
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter}\n---\nbody\n")
    with pytest.raises(AssertionError, match=message):
        _skill_frontmatter(skill_md)


def test_commands_present():
    cmd_dir = ROOT / "commands/kimi"
    names = {p.stem for p in cmd_dir.glob("*.md")}
    assert {"status", "consult", "review", "delegate", "dry-run"} <= names


def test_skill_routing_steers_long_work_to_async():
    """#338: the collaborating-with-kimi routing steers work that can exceed the sync
    deadline to the matching _async tool with a stated precedence, so the sync and async
    rows no longer both claim the same long-running workload; the reference files carry the
    concrete shapes rather than the vague pre-#338 threshold."""

    # Normalize whitespace: these docs use semantic line breaks, so a pinned phrase can
    # wrap across a newline in the source.
    def _flat(path):
        return " ".join((ROOT / path).read_text().split())

    skill = _flat("skills/collaborating-with-kimi/SKILL.md")
    bg = _flat("skills/collaborating-with-kimi/references/background-jobs.md")
    aw = _flat("skills/collaborating-with-kimi/references/active-workflows.md")
    assert "prefer the matching `_async` tool" in skill
    assert "can exceed the synchronous deadline" in skill
    assert "can exceed the synchronous deadline" in aw
    # The vague pre-#338 threshold is replaced by the concrete deadline framing.
    assert "may outlast a useful synchronous wait" not in bg
    assert "can exceed the synchronous deadline" in bg
    # Each route's distinctive shape is named, so a generic-only reword can't pass this.
    for shape in ("repo-grounded", "whole-branch", "substantial"):
        assert shape in skill, shape  # the shape-named async routing row
        assert shape in aw, shape  # the per-section active-workflow caveats
        assert shape in bg, shape  # the background-jobs shape list


def test_slash_commands_note_async_for_long_work():
    """#338: each sync /kimi:* command prompt points at its async variant for work that
    can exceed the sync deadline — naming this route's shape and the deadline framing, not
    just the async tool name — so a user-invoked sync command still surfaces the steer."""
    cmd_dir = ROOT / "commands/kimi"
    for stem, async_tool, shape in (
        ("consult", "kimi_consult_async", "repo-grounded"),
        ("review", "kimi_review_changes_async", "whole-branch"),
        ("delegate", "kimi_delegate_async", "substantial"),
    ):
        text = " ".join((cmd_dir / f"{stem}.md").read_text().split())
        assert async_tool in text, stem
        assert shape in text, stem
        assert "can exceed the synchronous deadline" in text, stem


async def test_capabilities_match_registered_tools():
    caps = server.kimi_capabilities()
    advertised = set(caps["active_tools"]) | set(caps["free_tools"])
    tool_names = {t.name for t in await server.mcp.list_tools()}
    assert advertised == tool_names


def test_tool_error_codes_cover_every_tool_and_are_valid():
    """Each advertised tool has an error-code list, and every code is a real ErrorCode."""
    from typing import get_args

    from moonbridge.schemas import ErrorCode

    caps = server.kimi_capabilities()
    advertised = set(caps["active_tools"]) | set(caps["free_tools"])
    valid_codes = set(get_args(ErrorCode))
    assert set(server._TOOL_ERROR_CODES) == advertised
    for tool, codes in server._TOOL_ERROR_CODES.items():
        assert set(codes) <= valid_codes, tool


def test_delegate_async_command_present():
    cmd_dir = ROOT / "commands/kimi"
    names = {p.stem for p in cmd_dir.glob("*.md")}
    assert "delegate-async" in names


# --------------------------------------------------------------------------- #
# Distribution: a pinned git tag
# --------------------------------------------------------------------------- #
# moonbridge is not published to PyPI, so .mcp.json installs it from the public
# GitHub remote at a pinned tag instead. The marketplace manifest is unaffected: it
# still installs from a local checkout, and only the MCP server launch resolves
# remotely.

GIT_SOURCE = "git+https://github.com/briandconnelly/moonbridge.git"


def test_marketplace_manifest_installs_from_this_checkout():
    """The manifest exists so the plugin can be installed LOCALLY:
    `/plugin marketplace add <this directory>`. Its source stays "./" — pointing it at a
    git URL would advertise a distribution that does not exist yet."""
    manifest = _load_json(".claude-plugin/marketplace.json")
    assert manifest["name"] == "moonbridge"
    plugins = manifest["plugins"]
    assert [p["name"] for p in plugins] == ["moonbridge"]
    assert plugins[0]["source"] == "./"


def test_codex_marketplace_manifest_installs_from_this_checkout():
    """The repo itself is a Codex marketplace, with the plugin at its root."""
    manifest = _load_json(".agents/plugins/marketplace.json")
    assert manifest["name"] == "moonbridge"
    plugins = manifest["plugins"]
    assert [plugin["name"] for plugin in plugins] == ["moonbridge"]
    assert plugins[0]["source"] == {"source": "local", "path": "./"}
    assert plugins[0]["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_USE",
    }
    assert plugins[0]["category"] == "Developer Tools"


def test_shared_skill_uses_host_neutral_workflow_roles():
    """The skill bundled into both plugins must not tell Codex that it is Claude."""
    instruction_files = [
        ROOT / "skills/collaborating-with-kimi/SKILL.md",
        ROOT / "skills/collaborating-with-kimi/references/independent-attempt.md",
        ROOT / "skills/collaborating-with-kimi/references/review-revise.md",
    ]
    for path in instruction_files:
        assert "Claude" not in path.read_text(), path


def test_plugin_copy_describes_worktrees_as_defense_in_depth():
    """Install-surface copy must not imply that the worktree contains Kimi."""
    paths = (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "README.md",
        "commands/kimi/delegate.md",
        "commands/kimi/delegate-async.md",
        "skills/collaborating-with-kimi/SKILL.md",
    )
    for relpath in paths:
        text = (ROOT / relpath).read_text()
        assert "isolated worktree" not in text, relpath
        assert "isolated git worktree" not in text, relpath


def test_mcp_json_installs_from_the_pinned_git_remote():
    config = json.loads((ROOT / ".mcp.json").read_text())
    server_config = config["mcpServers"]["moonbridge"]
    assert server_config["command"] == "uvx"
    assert server_config["args"][0] == "--from"
    assert server_config["args"][-1] == "moonbridge-mcp"
    # No local path may survive the switch. A `--directory` launch silently runs whatever
    # is in the author's working tree, and does not resolve at all on any other machine.
    assert "--directory" not in server_config["args"]
    # The PyPI form would resolve against an index where this package is absent.
    assert not any("==" in a for a in server_config["args"])


def test_mcp_json_pins_the_tag_for_the_current_version():
    """The pinned tag tracks pyproject's version. If it lags, a release ships a
    .mcp.json that installs the PREVIOUS release — a silent downgrade for every
    installed user, with nothing in the launch output to show it happened."""
    config = json.loads((ROOT / ".mcp.json").read_text())
    args = config["mcpServers"]["moonbridge"]["args"]
    ref = args[args.index("--from") + 1]
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert ref == f"{GIT_SOURCE}@v{pyproject['project']['version']}"
