from cairnival.config import AgentConfig
from cairnival.tools import ToolRegistry, safe_name


def registry(tmp_path) -> ToolRegistry:
    cfg = AgentConfig()
    cfg.tools_timeout_seconds = 30
    return ToolRegistry(tmp_path / "tools", tmp_path / "workspace", cfg)


def test_write_discover_and_run_bash_tool(tmp_path):
    reg = registry(tmp_path)
    reg.write_tool(
        "greet", "greet someone", "bash", '#!/usr/bin/env bash\necho "hello, $1"'
    )
    # a fresh registry discovers what the first one wrote — the every-wake path
    reg2 = registry(tmp_path)
    found = reg2.discover()
    assert "greet" in found
    result = reg2.run_tool("greet", ["world"])
    assert result.ok
    assert "hello, world" in result.output


def test_python_tool_reads_stdin(tmp_path):
    reg = registry(tmp_path)
    reg.write_tool(
        "count",
        "count stdin lines",
        "python",
        "import sys\nprint(len(sys.stdin.read().splitlines()))",
    )
    result = reg.run_tool("count", [], stdin="a\nb\nc")
    assert result.ok
    assert result.output.strip() == "3"


def test_shell_runs_and_persists_in_workspace(tmp_path):
    reg = registry(tmp_path)
    assert reg.run_shell("echo built > marker.txt").ok
    assert (tmp_path / "workspace" / "marker.txt").read_text().strip() == "built"
    # a later command sees the file the earlier one made
    listed = reg.run_shell("cat marker.txt")
    assert "built" in listed.output


def test_denylist_refuses(tmp_path):
    reg = registry(tmp_path)
    reg.cfg.tools_denylist = ["rm -rf /"]
    result = reg.run_shell("rm -rf / --no-preserve-root")
    assert not result.ok
    assert "denylist" in result.note


def test_shell_can_be_disabled(tmp_path):
    reg = registry(tmp_path)
    reg.cfg.tools_shell_enabled = False
    result = reg.run_shell("echo hi")
    assert not result.ok
    assert "disabled" in result.note


def test_nonzero_exit_reported(tmp_path):
    reg = registry(tmp_path)
    result = reg.run_shell("exit 3")
    assert not result.ok
    assert result.exit_code == 3


def test_output_is_truncated(tmp_path):
    reg = registry(tmp_path)
    result = reg.run_shell("for i in $(seq 1 1000); do echo linenumber$i; done")
    rendered = result.render(limit=200)
    assert "truncated" in rendered


def test_safe_name():
    assert safe_name("My Cool Tool!") == "my-cool-tool"
    assert safe_name("   ") == "tool"
