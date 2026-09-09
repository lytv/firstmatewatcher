#!/usr/bin/env python3
"""Local read-only viewer for Firstmate worker sessions.

Discovers live task records and leftover Claude/Grok logs for this home.
Serves 127.0.0.1 only. Does not write fleet state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
VIEWER = HERE / "trace-viewer.html"


def default_fm_home() -> Path:
    env = os.environ.get("FM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    candidate = Path.home() / "tools/myai/firstmate"
    if (candidate / "state").is_dir():
        return candidate.resolve()
    return Path.cwd()
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*([^\s'\"\\]+)"
)
RESULT_CAP = 80_000
CLAUDE_SLASH_DOT = str.maketrans("/.", "--")
PARSE_CACHE: dict[tuple[str, int, int], dict] = {}


def redact(text: str) -> str:
    if not text:
        return text
    return SECRET_RE.sub(r"\1=***", text)


def cap(text: str) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= RESULT_CAP:
        return text, False
    return text[:RESULT_CAP] + "\ntruncated", True


def group_for(name: str) -> str:
    if name in ("Bash", "run_terminal_command"):
        return "bash"
    if name in ("TaskCreate", "TaskUpdate", "TaskList"):
        return "task"
    if name in ("Read", "Write", "Edit", "read_file"):
        return "file"
    if name == "ScheduleWakeup":
        return "wake"
    return "other"


def summary_for(name: str, inp: dict) -> str:
    if name in ("Bash", "run_terminal_command"):
        return str(inp.get("command") or inp.get("tool_name") or name).strip()
    if name == "TaskCreate":
        return str(inp.get("subject") or "")
    if name == "TaskUpdate":
        return f"#{inp.get('taskId', '?')} -> {inp.get('status', '?')}"
    if name in ("Read", "Write", "Edit", "read_file"):
        return str(inp.get("file_path") or inp.get("target_file") or "")
    if name == "Skill":
        return f"{inp.get('skill', '')} {inp.get('args', '')}".strip()
    if name == "ScheduleWakeup":
        return str(inp.get("reason") or inp.get("prompt") or "")
    if name == "ToolSearch":
        return str(inp.get("query") or "")
    if inp:
        return json.dumps(inp, ensure_ascii=False)[:240]
    return name


def parse_meta(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k] = v
    return out


def spawn_epoch(meta: dict[str, str]) -> int | None:
    gen = meta.get("spawn_gen", "")
    if not gen.startswith("s"):
        return None
    head = gen[1:].split(".", 1)[0]
    return int(head) if head.isdigit() else None


def is_firstmate_cwd(cwd: str, fm_home: Path) -> bool:
    if not cwd:
        return False
    try:
        real = str(Path(cwd).resolve())
    except OSError:
        real = cwd
    home = str(fm_home.resolve())
    tree = str((Path.home() / ".treehouse").resolve())
    if real == home or real.startswith(home + "/"):
        return True
    if real == tree or real.startswith(tree + "/"):
        return True
    return "/.treehouse/" in real


def claude_projects_root() -> Path:
    cfg = Path(os_env_claude())
    return cfg / "projects"


def os_env_claude() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))


def peek_jsonl_meta(path: Path) -> dict:
    session = path.stem
    cwd = ""
    ts = ""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            if i > 40:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("sessionId"):
                session = str(obj["sessionId"])
            if obj.get("cwd") and not cwd:
                cwd = str(obj["cwd"])
            if obj.get("timestamp") and not ts:
                ts = str(obj["timestamp"])
            if session and cwd:
                break
    st = path.stat()
    return {
        "session": session,
        "cwd": cwd,
        "ts": ts,
        "mtime": int(st.st_mtime),
        "size": st.st_size,
    }


def parse_claude_log(path: Path) -> dict:
    pending: dict[str, dict] = {}
    calls: list[dict] = []
    tokens = Counter()
    usage_records = 0
    session = path.stem
    cwd = ""
    tasks: dict[str, dict] = {}

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("sessionId"):
                session = str(obj["sessionId"])
            if obj.get("cwd") and not cwd:
                cwd = str(obj["cwd"])
            ts = obj.get("timestamp") or ""
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if usage:
                usage_records += 1
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ):
                    val = usage.get(key)
                    if isinstance(val, (int, float)):
                        tokens[key] += int(val)
                details = usage.get("output_tokens_details")
                if isinstance(details, dict) and isinstance(
                    details.get("thinking_tokens"), (int, float)
                ):
                    tokens["thinking_tokens"] += int(details["thinking_tokens"])
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    inp = part.get("input") if isinstance(part.get("input"), dict) else {}
                    name = str(part.get("name") or "unknown")
                    uid = str(part.get("id") or f"anon-{len(calls)}")
                    rec = {
                        "id": uid,
                        "n": len(calls) + 1,
                        "ts": ts,
                        "name": name,
                        "group": group_for(name),
                        "summary": redact(summary_for(name, inp)),
                        "input": json.loads(redact(json.dumps(inp, ensure_ascii=False))),
                        "result": "",
                        "is_error": False,
                        "truncated": False,
                    }
                    calls.append(rec)
                    pending[uid] = rec
                    if name == "TaskCreate":
                        tid = str(len(tasks) + 1)
                        tasks[tid] = {
                            "id": tid,
                            "subject": str(inp.get("subject") or ""),
                            "description": str(inp.get("description") or ""),
                            "status": "pending",
                            "events": [{"ts": ts, "kind": "create", "status": "pending"}],
                        }
                    elif name == "TaskUpdate":
                        tid = str(inp.get("taskId") or "")
                        status = str(inp.get("status") or "")
                        if tid not in tasks:
                            tasks[tid] = {
                                "id": tid,
                                "subject": "",
                                "description": "",
                                "status": status,
                                "events": [],
                            }
                        tasks[tid]["status"] = status or tasks[tid]["status"]
                        tasks[tid]["events"].append(
                            {"ts": ts, "kind": "update", "status": status}
                        )
                elif part.get("type") == "tool_result":
                    uid = str(part.get("tool_use_id") or "")
                    rec = pending.get(uid)
                    if rec is None:
                        continue
                    body = part.get("content")
                    if not isinstance(body, str):
                        body = json.dumps(body, ensure_ascii=False)
                    body, truncated = cap(redact(body))
                    rec["result"] = body
                    rec["truncated"] = truncated
                    rec["is_error"] = bool(part.get("is_error"))

    counts = Counter(c["name"] for c in calls)
    return {
        "runtime": "claude",
        "session": session,
        "file": str(path),
        "cwd": cwd,
        "mtime": int(path.stat().st_mtime),
        "size_bytes": path.stat().st_size,
        "usage_records": usage_records,
        "tokens": dict(tokens),
        "counts": dict(counts),
        "call_count": len(calls),
        "tasks": [
            tasks[k]
            for k in sorted(tasks, key=lambda x: int(x) if str(x).isdigit() else 10**9)
        ],
        "calls": calls,
        "token_note": "",
    }


def parse_grok_session(session_dir: Path) -> dict:
    summary_path = session_dir / "summary.json"
    cwd = ""
    title = session_dir.name
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            summary = {}
        info = summary.get("info") if isinstance(summary, dict) else {}
        if isinstance(info, dict) and info.get("cwd"):
            cwd = str(info["cwd"])
        title = str(summary.get("generated_title") or title)
    calls: list[dict] = []
    events = session_dir / "events.jsonl"
    pending: dict[str, dict] = {}
    if events.is_file():
        for obj in _iter_jsonl(events):
            kind = obj.get("type")
            ts = obj.get("ts") or obj.get("timestamp") or ""
            name = str(obj.get("tool_name") or "unknown")
            if kind == "tool_started":
                rec = {
                    "id": f"g{len(calls)}",
                    "n": len(calls) + 1,
                    "ts": ts,
                    "name": name,
                    "group": group_for(name),
                    "summary": name,
                    "input": {"tool_name": name},
                    "result": "",
                    "is_error": False,
                    "truncated": False,
                }
                calls.append(rec)
                pending[name] = rec
            elif kind == "tool_completed":
                rec = pending.get(name)
                if rec is None:
                    continue
                rec["result"] = (
                    f"outcome={obj.get('outcome')} duration_ms={obj.get('duration_ms')}"
                )
                rec["is_error"] = str(obj.get("outcome") or "") not in ("success", "")
    counts = Counter(c["name"] for c in calls)
    st = session_dir.stat()
    return {
        "runtime": "grok",
        "session": session_dir.name,
        "file": str(events if events.is_file() else session_dir),
        "cwd": cwd,
        "title": title,
        "mtime": int(st.st_mtime),
        "size_bytes": st.st_size,
        "usage_records": 0,
        "tokens": {},
        "counts": dict(counts),
        "call_count": len(calls),
        "tasks": [],
        "calls": calls,
        "token_note": "Grok events list tools, not token usage",
    }


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def live_tasks(fm_home: Path) -> list[dict]:
    state = fm_home / "state"
    if not state.is_dir():
        return []
    rows = []
    for meta_path in sorted(state.glob("*.meta")):
        meta = parse_meta(meta_path)
        rows.append(
            {
                "task": meta_path.stem,
                "runtime": meta.get("harness", "unknown"),
                "kind": meta.get("kind", ""),
                "cwd": meta.get("worktree") or meta.get("home") or "",
                "spawn_at": spawn_epoch(meta),
            }
        )
    return rows


def discover(fm_home: Path) -> list[dict]:
    live = live_tasks(fm_home)
    live_by_cwd: dict[str, list[dict]] = {}
    for row in live:
        if row["cwd"]:
            live_by_cwd.setdefault(str(Path(row["cwd"]).resolve()) if row["cwd"] else "", []).append(row)

    sessions: list[dict] = []

    root = claude_projects_root()
    if root.is_dir():
        for jsonl in root.glob("*/*.jsonl"):
            peek = peek_jsonl_meta(jsonl)
            cwd = peek["cwd"]
            if not is_firstmate_cwd(cwd, fm_home):
                continue
            try:
                cwd_real = str(Path(cwd).resolve())
            except OSError:
                cwd_real = cwd
            matches = live_by_cwd.get(cwd_real) or []
            live_row = None
            for cand in matches:
                spawn_at = cand.get("spawn_at")
                if spawn_at is None or peek["mtime"] >= spawn_at:
                    live_row = cand
                    break
            sid = f"claude:{jsonl.stem}"
            label_cwd = Path(cwd).name or cwd
            if live_row:
                label = f"[live] {live_row['kind']} {live_row['task']} · claude"
                source = "live"
                task = live_row["task"]
            else:
                label = f"[leftover] {label_cwd} · claude · {jsonl.stem[:8]}"
                source = "leftover"
                task = ""
            sessions.append(
                {
                    "id": sid,
                    "runtime": "claude",
                    "source": source,
                    "task": task,
                    "label": label,
                    "cwd": cwd,
                    "file": str(jsonl),
                    "mtime": peek["mtime"],
                    "session": peek["session"],
                }
            )

    grok_root = Path.home() / ".grok" / "sessions"
    if grok_root.is_dir():
        for enc in grok_root.iterdir():
            if not enc.is_dir():
                continue
            cwd = unquote(enc.name)
            if not is_firstmate_cwd(cwd, fm_home):
                continue
            try:
                cwd_real = str(Path(cwd).resolve())
            except OSError:
                cwd_real = cwd
            for session_dir in enc.iterdir():
                if not session_dir.is_dir() or not (session_dir / "summary.json").is_file():
                    continue
                st = session_dir.stat()
                matches = live_by_cwd.get(cwd_real) or []
                live_row = None
                for cand in matches:
                    spawn_at = cand.get("spawn_at")
                    if spawn_at is None or int(st.st_mtime) >= spawn_at:
                        live_row = cand
                        break
                title = session_dir.name[:8]
                try:
                    summary = json.loads(
                        (session_dir / "summary.json").read_text(encoding="utf-8", errors="replace")
                    )
                    title = str(summary.get("generated_title") or title)
                except (OSError, json.JSONDecodeError):
                    pass
                if live_row:
                    label = f"[live] {live_row['kind']} {live_row['task']} · grok"
                    source = "live"
                    task = live_row["task"]
                else:
                    label = f"[leftover] {Path(cwd).name or cwd} · grok · {title}"
                    source = "leftover"
                    task = ""
                sessions.append(
                    {
                        "id": f"grok:{session_dir.name}",
                        "runtime": "grok",
                        "source": source,
                        "task": task,
                        "label": label,
                        "cwd": cwd,
                        "file": str(session_dir),
                        "mtime": int(st.st_mtime),
                        "session": session_dir.name,
                    }
                )

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def parse_session(entry: dict) -> dict:
    path = Path(entry["file"])
    st = path.stat()
    key = (str(path), int(st.st_mtime), st.st_size)
    cached = PARSE_CACHE.get(key)
    if cached is not None:
        out = dict(cached)
    elif entry["runtime"] == "claude":
        out = parse_claude_log(path)
        PARSE_CACHE[key] = out
    elif entry["runtime"] == "grok":
        out = parse_grok_session(path)
        PARSE_CACHE[key] = out
    else:
        raise ValueError(entry["runtime"])
    out = dict(out)
    out["id"] = entry["id"]
    out["source"] = entry["source"]
    out["task"] = entry["task"]
    out["label"] = entry["label"]
    return out


class Handler(BaseHTTPRequestHandler):
    fm_home: Path

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            if not VIEWER.is_file():
                self._send(500, b"missing trace-viewer.html", "text/plain; charset=utf-8")
                return
            self._send(200, VIEWER.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/sessions.json":
            sessions = discover(self.fm_home)
            payload = json.dumps(
                {"home": str(self.fm_home), "count": len(sessions), "sessions": sessions},
                ensure_ascii=False,
            ).encode()
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if path == "/api/log.json":
            sessions = discover(self.fm_home)
            wanted = (qs.get("id") or [""])[0]
            entry = None
            if wanted:
                entry = next((s for s in sessions if s["id"] == wanted), None)
            elif sessions:
                entry = sessions[0]
            if entry is None:
                self._send(
                    404,
                    json.dumps({"error": "no session"}).encode(),
                    "application/json; charset=utf-8",
                )
                return
            payload = json.dumps(parse_session(entry), ensure_ascii=False).encode()
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local Firstmate session viewer.")
    parser.add_argument("--home", default=str(default_fm_home()), help="Firstmate home")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    fm_home = Path(args.home).expanduser().resolve()
    Handler.fm_home = fm_home
    found = discover(fm_home)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"trace-viewer {url}", flush=True)
    print(f"home {fm_home}", flush=True)
    print(f"sessions {len(found)}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
