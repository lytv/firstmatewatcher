#!/usr/bin/env python3
"""Read-only proof: join one Firstmate task record to its worker session log.

Does not spawn, steer, write fleet state, or print tool arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

CLAUDE_SLASH_DOT = str.maketrans("/.", "--")


def die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


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


def list_task_meta(state: Path) -> list[Path]:
    return sorted(p for p in state.glob("*.meta") if p.is_file())


def claude_slug(cwd: str) -> str:
    return cwd.translate(CLAUDE_SLASH_DOT)


def claude_projects_root() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return Path(cfg) / "projects"


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def record_cwd(obj: dict) -> str | None:
    cwd = obj.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def claude_sessions_for(worktree: str, spawn_at: int | None) -> tuple[list[Path], list[str]]:
    root = claude_projects_root() / claude_slug(worktree)
    warnings: list[str] = []
    if not root.is_dir():
        return [], [f"no claude project dir {root}"]
    matched: list[tuple[float, Path]] = []
    skipped_prior = 0
    for path in root.glob("*.jsonl"):
        if not path.is_file():
            continue
        saw_cwd = False
        for obj in iter_jsonl(path):
            cwd = record_cwd(obj)
            if cwd == worktree:
                saw_cwd = True
                break
        if not saw_cwd:
            continue
        mtime = path.stat().st_mtime
        if spawn_at is not None and mtime < spawn_at:
            skipped_prior += 1
            continue
        matched.append((mtime, path))
    if skipped_prior:
        warnings.append(
            f"excluded {skipped_prior} older claude session(s) on the same copy path (spawn-time cut)"
        )
    matched.sort()
    return [p for _, p in matched], warnings


def fold_claude(path: Path) -> dict:
    tokens = Counter()
    tools = Counter()
    session_id = path.stem
    turns = 0
    for obj in iter_jsonl(path):
        if obj.get("sessionId"):
            session_id = str(obj["sessionId"])
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if isinstance(usage, dict):
            turns += 1
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
            if isinstance(details, dict):
                think = details.get("thinking_tokens")
                if isinstance(think, (int, float)):
                    tokens["thinking_tokens"] += int(think)
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    name = part.get("name") or "unknown"
                    tools[str(name)] += 1
    return {
        "session": session_id,
        "file": str(path),
        "usage_records": turns,
        "tokens": dict(tokens),
        "tools": dict(tools),
    }


def grok_sessions_for(worktree: str, spawn_at: int | None) -> tuple[list[Path], list[str]]:
    from urllib.parse import quote

    root = Path.home() / ".grok" / "sessions" / quote(worktree, safe="")
    warnings: list[str] = []
    if not root.is_dir():
        # Grok encodes the cwd; also try with trailing slash stripped/added
        alt = Path.home() / ".grok" / "sessions" / quote(worktree.rstrip("/") + "/", safe="")
        root = alt if alt.is_dir() else root
    if not root.is_dir():
        return [], [f"no grok session dir for this copy path"]
    matched: list[tuple[float, Path]] = []
    skipped = 0
    for session in root.iterdir():
        summary = session / "summary.json"
        if not summary.is_file():
            continue
        try:
            info = json.loads(summary.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        cwd = (info.get("info") or {}).get("cwd") if isinstance(info, dict) else None
        if cwd and os.path.normpath(cwd) != os.path.normpath(worktree):
            continue
        mtime = summary.stat().st_mtime
        if spawn_at is not None and mtime < spawn_at:
            skipped += 1
            continue
        matched.append((mtime, session))
    if skipped:
        warnings.append(
            f"excluded {skipped} older grok session(s) on the same copy path (spawn-time cut)"
        )
    matched.sort()
    return [p for _, p in matched], warnings


def fold_grok(session_dir: Path) -> dict:
    tools = Counter()
    events = session_dir / "events.jsonl"
    if events.is_file():
        for obj in iter_jsonl(events):
            if obj.get("type") == "tool_started" and obj.get("tool_name"):
                tools[str(obj["tool_name"])] += 1
    return {
        "session": session_dir.name,
        "file": str(events if events.is_file() else session_dir),
        "usage_records": 0,
        "tokens": {},
        "tools": dict(tools),
        "token_note": "no usage fields in this grok session summary or events",
    }


def join_task(home: Path, task_id: str) -> dict:
    meta_path = home / "state" / f"{task_id}.meta"
    if not meta_path.is_file():
        die(f"no task record {meta_path}", 1)
    meta = parse_meta(meta_path)
    worktree = meta.get("worktree") or meta.get("home")
    harness = meta.get("harness", "unknown")
    if not worktree:
        die(f"{task_id} has no isolated copy path", 1)
    spawn_at = spawn_epoch(meta)
    warnings: list[str] = []
    sessions: list[Path] = []
    fold = None
    if harness.startswith("claude"):
        sessions, warnings = claude_sessions_for(worktree, spawn_at)
        fold = fold_claude
    elif harness.startswith("grok"):
        sessions, warnings = grok_sessions_for(worktree, spawn_at)
        fold = fold_grok
    else:
        die(f"proof CLI has no adapter for runtime {harness!r} yet", 1)
    if not sessions:
        die(f"no session log joined for {task_id} ({harness})", 1)
    if len(sessions) > 1:
        warnings.append(
            f"{len(sessions)} sessions passed the spawn-time cut; using the newest"
        )
    chosen = sessions[-1]
    folded = fold(chosen)
    return {
        "task": task_id,
        "runtime": harness,
        "kind": meta.get("kind", ""),
        "copy": worktree,
        "spawn_epoch": spawn_at,
        **folded,
        "warnings": warnings,
    }


def pick_default_task(home: Path) -> str:
    metas = list_task_meta(home / "state")
    if not metas:
        die("no live task records in state/", 1)
    if len(metas) == 1:
        return metas[0].stem
    die(
        "multiple task records; pass --id. have: " + ", ".join(p.stem for p in metas),
        1,
    )


def default_fm_home() -> Path:
    env = os.environ.get("FM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    candidate = Path.home() / "tools/myai/firstmate"
    if (candidate / "state").is_dir():
        return candidate.resolve()
    return Path.cwd()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Join one Firstmate task to its worker session log (read-only proof)."
    )
    parser.add_argument("--home", default=str(default_fm_home()))
    parser.add_argument("--id", help="task id; default is the only live record")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    home = Path(args.home).resolve()
    task_id = args.id or pick_default_task(home)
    report = join_task(home, task_id)
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    print(f"task:     {report['task']}")
    print(f"runtime:  {report['runtime']}")
    print(f"kind:     {report['kind']}")
    print(f"copy:     {report['copy']}")
    print(f"session:  {report['session']}")
    print(f"file:     {report['file']}")
    tokens = report.get("tokens") or {}
    if tokens:
        print("tokens:")
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "thinking_tokens",
        ):
            if key in tokens:
                print(f"  {key}: {tokens[key]}")
        print(f"  usage_records: {report.get('usage_records', 0)}")
    else:
        print(f"tokens:   none ({report.get('token_note', 'no usage fields')})")
    tools = report.get("tools") or {}
    print("tools:")
    if not tools:
        print("  (none)")
    else:
        for name, n in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {name}: {n}")
    for w in report.get("warnings") or []:
        print(f"warning:  {w}")


if __name__ == "__main__":
    main()
