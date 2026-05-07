# Design: Non-Interactive CLI Mode

**Status: planned · 2026-05-03**

`python -m app.cli --session arch --command "dispatch X" --json` for
script-friendly use.

## Why

Today the CLI is interactive only — `_run_cli()` in `main_window.py`
loops on `input()`. Agents and shell scripts can't drive it
non-interactively without one of:

- Spawning a Python REPL and feeding `dispatch_input(...)` calls
- Using the HTTP gateway (extra service, extra setup)
- Talking to a future MCP server (extra protocol)

A simple `--command` mode covers the common case: "run one dispatcher
command, print the result, exit." Useful for cron jobs, one-shot
scripts, CI integration, and as the cheapest path for any external
agent that already shells out.

## Surface

```
python -m app.cli --session <id> --command "<text>" [--json] [--quiet]
```

Or shorthand:

```
python -m app.cli -s <id> -c "<text>" [-j] [-q]
```

### Options

| Flag                         | Purpose                                                     |
|------------------------------|-------------------------------------------------------------|
| `--session, -s <id>`         | Session id (default `default`)                              |
| `--command, -c <text>`       | One dispatcher command to run                               |
| `--json, -j`                 | Emit the full event dict as JSON (default: human text)      |
| `--quiet, -q`                | Suppress the human banner; just print the output            |
| `--from-stdin`               | Read the command from stdin instead of `--command`          |
| `--timeout <sec>`            | Cap the call (default: none for non-blocking commands; bounded for `do`/`wait`) |

### Exit codes

| Code | Meaning                                                                  |
|------|--------------------------------------------------------------------------|
| 0    | Command ran and returned without an error reason                         |
| 1    | Argument / parse error                                                   |
| 2    | Command emitted a `reason` (see `error-vocabulary.md`); details on stderr|
| 130  | Cancelled (Ctrl-C)                                                       |

`--json` always prints to stdout; the human banner (one short status
line) goes to stderr unless `--quiet`. Lets you pipe `--json` cleanly:

```bash
python -m app.cli -c doctor --json | jq .output
```

## Examples

### One-shot dispatch

```bash
$ python -m app.cli -c "doctor"
=== Doctor ===
Ollama: connected · 0.12.7
Lanes: reasoning · coding ready, vision missing, unfiltered optional
Workspace: C:/Users/micha/Desktop/localaiproject
…
```

### Machine-readable

```bash
$ python -m app.cli -c "executions 5" --json | jq .output
"Executions for 'default' (last 5)\n…"

$ python -m app.cli -c "wait inbox coder 60" --json | jq -r .reason
# (empty if drained, or "timeout" if not)
```

### Pipe long input

```bash
$ git diff main | python -m app.cli -s reviewer --from-stdin -c "review this diff:"
# combines `--command` with stdin appended
```

### From CI

```bash
#!/bin/bash
set -e
python -m app.cli -s ci -c "workspace $PWD" --json
python -m app.cli -s ci -c "do @coder run the smoke tests" --timeout 300 --json
```

## Implementation

### Module: `app/cli.py`

A thin argparse wrapper that:
1. Initialises storage (so we use the same backend as the GUI process)
2. Calls `dispatch_input(command, session_id=session)`
3. Renders the resulting event dict as text or JSON
4. Maps the `reason` (if any) to an exit code

```python
# app/cli.py (sketch)
import argparse, json, sys
from app.services.command_dispatcher import dispatch_input

def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("-s", "--session", default="default")
    parser.add_argument("-c", "--command", default=None)
    parser.add_argument("--from-stdin", action="store_true")
    parser.add_argument("-j", "--json", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args(argv)

    text = args.command or ""
    if args.from_stdin:
        stdin_text = sys.stdin.read().strip()
        text = (text + "\n\n" + stdin_text).strip() if text else stdin_text
    if not text:
        parser.error("either --command or --from-stdin (or both) required")

    # TODO timeout handling for do/wait verbs
    event = dispatch_input(text, session_id=args.session)

    if args.json:
        print(json.dumps(event, ensure_ascii=False))
    else:
        if not args.quiet:
            print(f"[{event.get('action')} on {event.get('session_id')}]", file=sys.stderr)
        if event.get("output"):
            print(event["output"])

    if event.get("reason"):
        print(f"reason: {event['reason']}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### Integration points

- Same storage backend as the GUI; use `MEMORY_BACKEND` env var to opt
  into DuckDB (single file shared with the GUI process — DuckDB's lock
  serialises us correctly)
- Reuse the `app.services.command_dispatcher` module unchanged
- Do **not** subscribe to the bus from the CLI; one-shot use doesn't
  need it

### Async / streaming

For one-shot use the synchronous `dispatch_input` is enough. Long-running
goal verbs (`do`, `wait inbox auto`) block the process, which is the
right behaviour for shell use.

If a future round adds streaming output (e.g. live token output from
`chat`), the CLI gains `--stream` that hooks into the bus and prints
events as they arrive, then exits when the dispatcher event comes
through.

### Existing entry point

`app/main.py` is the GUI entry. Keep it; add `app/cli.py` alongside.
The GUI's `_run_cli()` interactive loop stays as the fallback when no
GUI framework is installed.

## Tests

`tests/test_cli.py`:
- argparse parsing (each option)
- `--command` with a no-op command (`help`) returns exit 0
- `--from-stdin` reads correctly
- `--json` produces valid JSON parsed back into a dict
- A command that emits a `reason` returns exit 2 (gated on the error
  vocabulary work landing)
- Timeout flag (mock the dispatcher to sleep)

## Anti-goals

- This isn't a REPL replacement. The interactive CLI in `main_window.py`
  is fine for humans.
- It doesn't reimplement any dispatcher routing. One thin entry point,
  one call.
- It doesn't manage long-lived sessions; each invocation is one-shot.
  Use the gateway for persistent connections.

## Open questions

- Should `--session` auto-create sessions that don't exist yet? Today
  `dispatch_input` creates implicitly via the storage layer; consistent
  to keep that behaviour.
- Do we want a `--watch` mode that subscribes to the bus and prints
  events for a given session forever (Ctrl-C to exit)? Probably yes,
  but lower priority — `curl /events` does it for free if the gateway
  is running.
- `--stdin-as-pipeline` to run multiple commands separated by newlines?
  Maybe; `xargs` already does it for the simple case.
