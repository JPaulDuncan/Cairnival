from cairnival.config import AgentConfig
from cairnival.memory import DEFAULT_SOUL
from cairnival.tools import ToolRegistry, safe_name


def test_default_soul_advertises_new_capabilities():
    soul = DEFAULT_SOUL.format(name="rustle")
    low = " ".join(soul.lower().split())  # collapse wrapping whitespace
    # hands: shell + installing software
    assert "shell command" in low
    assert "install software" in low
    # writing tools that persist across wakes
    assert "write your own tools" in low
    assert "every future wake" in low
    # treasury co-sign and federation are named
    assert "one key of" in low
    assert "federation" in low


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


def test_source_update_and_delete(tmp_path):
    reg = registry(tmp_path)
    reg.write_tool("greet", "hi", "bash", '#!/usr/bin/env bash\necho hello')
    assert "echo hello" in reg.source("greet")

    reg.update_source("greet", "#!/usr/bin/env bash\necho HI", description="louder")
    # a fresh registry (next wake) sees the edit
    reg2 = registry(tmp_path)
    reg2.discover()
    assert "echo HI" in reg2.source("greet")
    assert reg2.tools["greet"].description == "louder"
    assert reg2.run_tool("greet", []).output.strip() == "HI"

    assert reg2.delete("greet") is True
    assert "greet" not in registry(tmp_path).discover()


def test_reset_wipes_knowledge_keeps_identity(tmp_path):
    from cairnival.federation import Identity
    from cairnival.memory import Memory
    from cairnival.specimens import Specimen, save as save_specimen

    memory = Memory(tmp_path, "rustle")
    memory.ensure()
    Identity.load_or_create(memory.keys_dir, "rustle")
    key_before = (memory.keys_dir / "ed25519.key").read_text()
    save_specimen(Specimen(id="SP-0001", agent="rustle", title="t", body="b"), memory.specimens_dir)
    memory.journal_append("a wake happened")
    memory.remember_append("a memory")
    memory.save_state({"wakes": 12})

    memory.reset()

    assert not list(memory.specimens_dir.glob("SP-*.md"))
    assert memory.load_state().get("wakes", 0) == 0
    assert not memory.remember_path.exists()
    # identity and soul are kept by default
    assert (memory.keys_dir / "ed25519.key").read_text() == key_before
    assert memory.soul_path.exists()


def test_reset_can_mint_new_identity(tmp_path):
    from cairnival.federation import Identity
    from cairnival.memory import Memory

    memory = Memory(tmp_path, "rustle")
    memory.ensure()
    Identity.load_or_create(memory.keys_dir, "rustle")
    key_before = (memory.keys_dir / "ed25519.key").read_text()
    memory.reset(new_identity=True)
    Identity.load_or_create(memory.keys_dir, "rustle")  # lazily recreated
    assert (memory.keys_dir / "ed25519.key").read_text() != key_before


def test_safe_name():
    assert safe_name("My Cool Tool!") == "my-cool-tool"
    assert safe_name("   ") == "tool"


# -- per-tool sub-surface: run history, files, manifest --------------------

def test_run_history_is_recorded_and_bounded(tmp_path):
    reg = registry(tmp_path)
    reg.write_tool("counter", "counts args", "bash", 'echo "$#"')
    assert reg.run_history("counter") == []
    reg.run_tool("counter", ["a", "b"], by="you")
    reg.run_tool("counter", ["x"], by="agent")
    hist = reg.run_history("counter")
    assert len(hist) == 2
    assert hist[0]["by"] == "agent" and hist[0]["args"] == "x"  # newest first
    assert hist[1]["by"] == "you" and hist[1]["ok"] is True
    # bounded to 25
    for i in range(30):
        reg.run_tool("counter", [str(i)])
    assert len(reg.run_history("counter")) == 25


def test_files_and_manifest_introspection(tmp_path):
    reg = registry(tmp_path)
    reg.write_tool("greet", "hi", "python", "print('hi')")
    names = {f["name"] for f in reg.files("greet")}
    assert {"run.py", "tool.json"} <= names
    assert any(f["entry"] for f in reg.files("greet") if f["name"] == "run.py")
    assert '"name": "greet"' in reg.manifest_text("greet")
