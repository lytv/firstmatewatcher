# Firstmate Watcher

Local viewer for Firstmate worker sessions. It lists tool calls, tasks, and token counts from logs on your machine. It does not send data to the network. It binds to `127.0.0.1` only.

License: MIT. See `LICENSE`.

## What you need

- Python 3.9 or newer
- A Firstmate home (the folder that contains `state/` and `data/`)
- Claude and/or Grok session logs on this computer

No extra Python packages.

## Start the viewer

```bash
export FM_HOME=/Users/lytv/tools/myai/firstmate
python3 fm-trace-server.py --home "$FM_HOME"
```

Then open http://127.0.0.1:8765/

The server loads every matching session at start:

- Live Firstmate workers (`$FM_HOME/state/*.meta`)
- Leftover Claude logs for isolated copies
- Leftover Grok sessions for isolated copies

Use the left list to pick a session. The row labeled **this session** is the Grok session that started the server. Type in **Find a session** to filter by name, runtime, or path. Use All, This, Live, or Leftover to narrow the list. The page refreshes every 3 seconds.

### Useful flags

```bash
python3 fm-trace-server.py --home "$FM_HOME" --host 127.0.0.1 --port 8765
```

| Flag | Default | Meaning |
|---|---|---|
| `--home` | `$FM_HOME`, or `~/tools/myai/firstmate` if that folder exists | Firstmate home to scan |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8765` | Bind port |

Stop the server with Ctrl+C.

## How to read the page

1. Pick a session in **Session**.
2. `[live]` means Firstmate still has a task record. `[leftover]` means the worker log remains after cleanup.
3. Open **Bash** for every shell command. Click a row for input and result.
4. Open **Tasks** for TaskCreate / TaskUpdate.
5. Token counts come from Claude usage records. Cache read is not the same as spend. Grok lists tools, not tokens.

Obvious secret patterns (`api_key`, `token`, `password`, `bearer`) are redacted in the page. Long results stop at 80,000 characters.

## One-task CLI (no browser)

```bash
export FM_HOME=/Users/lytv/tools/myai/firstmate
python3 fm-trace-join.py --home "$FM_HOME"
python3 fm-trace-join.py --home "$FM_HOME" --id <task-id>
python3 fm-trace-join.py --home "$FM_HOME" --id <task-id> --json
```

This joins one live task record to its worker log. It fails if that task was already cleaned up, even when the worker log is still on disk. Use the HTML viewer for leftover sessions.

## Files

| File | Role |
|---|---|
| `fm-trace-server.py` | Local HTTP server and session discovery |
| `trace-viewer.html` | Page UI |
| `fm-trace-join.py` | One-task CLI |

## Limits

- Claude and Grok only. Cursor, Codex, Pi, and others are not joined yet.
- After Firstmate cleanup, the task record is gone. The viewer can still open leftover logs.
- Isolated copy paths can be reused. Live joins also cut by spawn time so an old log on the same path is not billed to a new task.
