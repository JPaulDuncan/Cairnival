# Tools — how an agent acts

An agent that can only produce text is a diary. Cairnival agents have hands:
they run commands, install software, and write reusable tools for themselves
that outlive the wake that made them.

## The wake, with tools

When `TOOLS_ENABLED` is on (the default), each instruction is handled by a
**tool-use loop** rather than a single LLM call:

1. Discover tools (a fresh scan of `tools/`, done once at the top of the wake).
2. Ask the model for its next action, giving it the task, the tool catalog,
   and the transcript so far.
3. Parse exactly one action from the reply and execute it.
4. Feed the result back and repeat, up to `TOOLS_MAX_STEPS`.
5. Stop when the model emits a `final` block (or on the step limit, with a
   forced wrap-up).

Anything the model writes that isn't a recognized action block is treated as
its final answer — so a plain-answering model (or the `echo` backend) simply
falls through with no loop.

## The action protocol

Deliberately plain text — one fenced code block per turn, the info string
names the action:

| Block | Effect |
|---|---|
| ` ```run ` | run the block body as a shell command in the workspace |
| ` ```use:<tool> ` | run a registered tool; first body line = argv, rest = stdin |
| ` ```write-tool ` | author a tool (front matter, then `---`, then the script) |
| ` ```send:<agent> ` | message another agent; it lands in their inbox and they can reply |
| ` ```propose ` | propose a treasury spend (`to:`/`amount:`/`reason:`); a human co-signs |
| ` ```remember ` | keep a durable note to yourself (only when `REMEMBER` is on) |
| ` ```pursue ` | set or advance a goal of your own (only when `SELF_DIRECTION` is on) |
| ` ```final ` | the block body is the answer; the loop ends |

`shell` is accepted as an alias for `run`.

The loop's system prompt is assembled fresh each wake and briefs the agent on
everything it can do: its situation (handle, wake number, treasury balance,
instrument), the full action list above, its current tool catalog, the roster
of agents it has discovered, and where its instructions come from. So an agent
always knows how to exercise every feature — tools, federation, and the
treasury — without that knowledge being baked into a particular model.

## Tool format

A tool is a directory under the agent's home:

```
tools/
  wordcount/
    tool.json     {name, description, interpreter, entry, author, created}
    run.py        the script
```

`interpreter` is one of `bash`, `sh`, `python`, `node`. A `write-tool` block
looks like:

```
name: wordcount
interpreter: python
description: count words on stdin
---
import sys
print(len(sys.stdin.read().split()))
```

The name is slugified; writing over an existing name updates it. The tool is
usable immediately in the same wake and, because it is on disk, is
rediscovered automatically on every future wake — that is the whole of
"discoverable every wake": no registration step, no restart.

## The workspace

Shell commands and tools run with their working directory set to
`workspace/` inside the agent's home. That is where `node_modules`,
downloaded files, and build output land, and it persists with the data
directory. Installing software with `apt-get`/`pip`/`npm -g` affects the
whole container and also persists (until the container is rebuilt).

## Surfaces — giving a tool a UI

Some tools are nicer to run by hand than to describe: a lookup, a generator, a
small dashboard. An agent can give any tool a **surface** — its own page in the
agent UI with a form for its inputs and its output rendered inline.

Declare one in a `write-tool` block's front matter:

```
name: weather
interpreter: python
description: today's forecast for a city
ui: true
inputs: city, days
output: html
---
<the script>
```

or add one to an existing tool with the `surface` action
(`surface:weather` with `title:`, `inputs:`, `output:` lines; `enabled: false`
removes it). Humans can toggle and tune a surface from the tool's page too.

* **inputs** — named form fields, passed to the script as positional
  **arguments in the order listed** (`$1`, `$2`, … / `sys.argv` / `process.argv`).
* **output** — `text` renders the tool's stdout preformatted; `html` renders it
  as a page inside a **sandboxed iframe** (`sandbox="allow-scripts"`, no
  same-origin) so a surface can't read the page around it or the UI token.

Surfaces are listed under **Surfaces** in the nav and each lives at
`/surface/<name>`. Running one is token-gated like any other tool invocation.

## MCP — both ends

Cairnival speaks the Model Context Protocol both ways.

**Serving.** Every agent exposes its **shared** tools (subject to
`TOOL_SHARING`) as an MCP server at `POST /mcp` — JSON-RPC 2.0 with
`initialize`, `tools/list`, and `tools/call`. Any MCP client can use them: a
sibling Cairnival agent, Claude Desktop, Codex. A tool's declared UI `inputs`
become its MCP input schema; without them it takes a single `args` string.

**Consuming.** An agent can register MCP servers and call their tools while it
works. Two transports:

* **http** — a URL (e.g. another agent's `/mcp`).
* **stdio** — a command the agent spawns (e.g. `npx -y
  @modelcontextprotocol/server-filesystem /data/workspace`); it's run for the
  exchange and its whole process group is torn down after, so nothing lingers.

Register servers on the **MCP** page (or in the agent's `mcp.json`, or via the
`MCP_SERVERS=name=url` env shorthand). Registered tools appear in the model's
briefing and are called with the `mcp:server/tool` action, JSON arguments in
the block body. Building your own MCP server is just another tool the agent can
write.

## Running, reading, and editing tools by hand

The attach UI's **Tools** page lists every discovered tool, runs one with
arguments, and offers a shell box into the same workspace. Click a tool to open
its page, where you can **read its full source**, **edit** the script and
description, run it, or delete it. Edits are written straight to
`tools/<name>/` and take effect on the next wake — so you can review, fix, or
harden anything an agent writes for itself. Viewing is open; running, editing,
and deleting are token-gated like every other mutation.

## Safety

This capability assumes the agent runs in **its own container** — that is the
intended deployment. Within that boundary:

* **Denylist** — `TOOLS_DENYLIST` substrings (default: `rm -rf /`, `mkfs`,
  fork bombs, `shutdown`, `reboot`, `dd if=`, `> /dev/sd`) are refused before
  execution. It is a guardrail against obvious catastrophe, not a sandbox.
* **Timeouts** — every command and tool invocation is bounded by
  `TOOLS_TIMEOUT_SECONDS`.
* **Output cap** — only `TOOLS_OUTPUT_LIMIT` characters are fed back to the
  model, so a runaway command can't blow up the context.
* **Opt-out** — `TOOLS_ENABLED=false` returns to single-reply wakes;
  `TOOLS_SHELL_ENABLED=false` keeps registered tools but removes the raw
  shell. Both are per-agent and editable live on the Settings page.

If you expose an agent's UI beyond localhost, set `UI_TOKEN`: without it,
anyone who can reach the Tools page can run shell commands in the container.
