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
| ` ```final ` | the block body is the answer; the loop ends |

`shell` is accepted as an alias for `run`.

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

## Running tools by hand

The attach UI's **Tools** page lists every discovered tool, runs one with
arguments, and offers a shell box into the same workspace — all token-gated
like every other mutation. The last output is shown inline. This is the
fastest way to see what the agent built.

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
