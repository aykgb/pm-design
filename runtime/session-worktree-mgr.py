#!/usr/bin/env python3
"""
session-worktree-mgr.py

Fixed worktree pool + persistent OpenCode session dispatcher.

Design:
- Keep OpenCode API directory = worktree path.
- Worktrees are long-lived pool resources.
- pool init/repair performs full warm-up:
  - create/reuse wt_N
  - COPY main .opencode/node_modules into wt_N/.opencode/node_modules
  - create/reuse wt_N-Agent sessions
  - register sessions to sidecar
  - persist session ids in .state/wt_N.state
- prepare only grabs an idle initialized worktree and checks out task branch.
- dispatch uses session id from state, not title search.
- release resets worktree and marks idle; it does not delete sessions.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeGuard, cast

DEFAULT_AGENTS = ("Daedalus", "Morpheus", "Themis", "QA")
PROG = "python3 scripts/session-worktree-mgr.py"


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env_extra:
        merged_env.update(env_extra)
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        text=True,
        capture_output=capture,
        check=check,
    )


def require_cmd(name: str) -> None:
    if shutil.which(name) is None:
        fail(f"command not found: {name}")


def git(cwd: Path, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args], capture=capture, check=check)


def repo_root(cwd: Path | None = None) -> Path:
    cwd = cwd or Path.cwd()
    try:
        out = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture=True)
    except subprocess.CalledProcessError:
        fail("not inside a git repository")
    return Path(out.stdout.strip()).resolve()


def parse_agents(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_AGENTS)
    agents = [x.strip() for x in value.split(",") if x.strip()]
    if not agents:
        fail("empty --agents")
    for agent in agents:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", agent):
            fail(f"invalid agent name: {agent}")
    return agents


def normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


@dataclass(frozen=True)
class Config:
    repo: Path
    pool_dir: Path
    pool_size: int
    max_worktrees: int
    base_ref: str
    op_server: str
    sidecar: str
    op_host: str
    op_port: int
    sidecar_host: str
    sidecar_port: int
    log_dir: Path
    http_timeout: int
    pm_session_id: str = ""  # per-PM-session isolation of main agents

    @classmethod
    def load(cls) -> Config:
        root = repo_root()
        op_port = int(env("OP_PORT", "4097"))
        sidecar_port = int(env("SIDECAR_PORT", "4107"))
        pm_sid = ""
        try:
            info_path = root / ".pm" / "pm-session-info.json"
            if info_path.exists():
                info = json.loads(info_path.read_text(encoding="utf-8"))
                pm_sid = str(info.get("current_session_id") or "")
        except Exception:
            pass
        return cls(
            repo=root,
            pool_dir=Path(env("WORKTREE_POOL_DIR", str(Path.home() / ".worktrees" / root.name))).expanduser().resolve(),
            pool_size=int(env("POOL_SIZE", "10")),
            max_worktrees=int(env("MAX_WORKTREES", "10")),
            base_ref=env("WORKTREE_BASE_REF", "origin/main"),
            op_server=env("OP_SERVER", f"http://127.0.0.1:{op_port}").rstrip("/"),
            sidecar=env("SIDECAR", f"http://127.0.0.1:{sidecar_port}").rstrip("/"),
            op_host=env("OP_HOST", "127.0.0.1"),
            op_port=op_port,
            sidecar_host=env("SIDECAR_HOST", "127.0.0.1"),
            sidecar_port=sidecar_port,
            log_dir=root / ".opencode" / "logs",
            http_timeout=int(env("OP_HTTP_TIMEOUT", "10")),
            pm_session_id=pm_sid,
        )


# ---------------- HTTP ----------------


def no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    expected: tuple[int, ...] = (200,),
    timeout: int | None = None,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    effective_timeout = timeout if timeout is not None else int(os.environ.get("OP_HTTP_TIMEOUT", "10"))
    try:
        with no_proxy_opener().open(req, timeout=effective_timeout) as res:
            payload = res.read()
            status = res.status
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if exc.code not in expected:
            fail(f"{method} {url} failed: HTTP {exc.code}\n{payload}")
        return payload
    except TimeoutError:
        fail(f"{method} {url} timed out after {effective_timeout}s.\nFor directory=worktree, run pool repair/init to prewarm the worktree.")
    except urllib.error.URLError as exc:
        fail(f"{method} {url} failed: {exc}")
    if status not in expected:
        text = payload.decode("utf-8", errors="replace")
        fail(f"{method} {url} failed: HTTP {status}\n{text}")
    if not payload:
        return None
    text = payload.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http_code(url: str, timeout: int = 2) -> int | None:
    try:
        with no_proxy_opener().open(url, timeout=timeout) as res:
            return int(res.status)
    except Exception:
        return None


# ---------------- state / git ----------------


@contextlib.contextmanager
def pool_lock(config: Config) -> Iterator[None]:
    config.pool_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = config.pool_dir / ".grab.lock"
    try:
        lock_dir.mkdir()
    except FileExistsError:
        fail(f"another pool operation is running: {lock_dir}")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_dir.rmdir()


def state_dir(config: Config) -> Path:
    return config.pool_dir / ".state"


def state_file(config: Config, wt_id: str) -> Path:
    return state_dir(config) / f"{wt_id}.state"


def read_state(config: Config, wt_id: str) -> dict[str, str]:
    return _read_state_file(state_file(config, wt_id))


def _read_state_file(path: Path) -> dict[str, str]:
    """Read a state file in the standard ``key=value`` format.

    Used by ``read_state`` (which routes by wt_id) and by callers that need
    to read a state file at a specific Path (e.g. a per-PM main.state that is
    NOT the current PM's main.state).
    """
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def _write_state_file(path: Path, values: dict[str, str]) -> None:
    """Write a standard ``key=value`` state file at an explicit path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def write_state(config: Config, wt_id: str, values: dict[str, str]) -> None:
    _write_state_file(state_file(config, wt_id), values)


def update_state(config: Config, wt_id: str, patch: dict[str, str]) -> dict[str, str]:
    state = read_state(config, wt_id)
    state.update(patch)
    state["updated_at"] = now_utc()
    write_state(config, wt_id, state)
    return state


def _tombstoned_sids(state: dict[str, str]) -> set[str]:
    """Parse ``state['deleted_session_ids']`` (comma-separated) into a set.

    Soft-deleted session IDs are recorded here by ``cmd_sessions_delete`` (when
    ``--hard`` is NOT passed) so that ``rewatch_all_sessions`` and
    ``ensure_session`` know to skip them. The on-disk state still keeps the
    ``*_session_id`` pointer intact (dispatch keeps working), but the
    tombstoned session id is excluded from sidecar re-watch and title-search
    re-attach.
    """
    raw = state.get("deleted_session_ids", "")
    return {x for x in raw.split(",") if x}


def _add_tombstone(config: Config, wt_id: str, sid: str) -> None:
    """Append ``sid`` to the state file's ``deleted_session_ids`` field.

    Works for both ``wt_N.state`` and ``xidi-minimal`` (which routes to the
    per-PM-scoped ``sessions/<pm_sid>/main.state`` via ``update_main_state``).
    Existing ``*_session_id`` fields are NOT touched (per spec: do not pop
    other fields — only mark the sid as soft-deleted).
    """
    if wt_id == "xidi-minimal":
        state = read_main_state(config)
        existing = _tombstoned_sids(state)
        existing.add(sid)
        update_main_state(
            config,
            {"deleted_session_ids": ",".join(sorted(existing))},
        )
        return
    state = read_state(config, wt_id)
    existing = _tombstoned_sids(state)
    existing.add(sid)
    update_state(
        config,
        wt_id,
        {"deleted_session_ids": ",".join(sorted(existing))},
    )


def _prune_tombstones(config: Config, wt_id: str, alive: set[str]) -> None:
    """Drop tombstoned sids that are no longer present in OpenCode.

    Called by ``cleanup_stale_sessions`` so the tombstone set does not grow
    unbounded as ``sessions delete`` accumulates over time. ``alive`` is the
    set of session ids that ``GET /session/{id}`` returned a payload for; any
    tombstoned sid not in ``alive`` is removed from the field.
    """
    if wt_id == "xidi-minimal":
        state = read_main_state(config)
        tombstoned = _tombstoned_sids(state)
        keep = tombstoned & alive
        if keep != tombstoned:
            update_main_state(
                config,
                {"deleted_session_ids": ",".join(sorted(keep)) if keep else ""},
            )
        return
    state = read_state(config, wt_id)
    tombstoned = _tombstoned_sids(state)
    keep = tombstoned & alive
    if keep != tombstoned:
        update_state(
            config,
            wt_id,
            {"deleted_session_ids": ",".join(sorted(keep)) if keep else ""},
        )


def wt_id_for_index(index: int) -> str:
    return f"wt_{index}"


def validate_wt_id(wt_id: str) -> None:
    if not re.fullmatch(r"wt_[0-9]+", wt_id):
        fail(f"invalid wt_id: {wt_id}, expected wt_N")


def path_for_wt(config: Config, wt_id: str) -> Path:
    validate_wt_id(wt_id)
    return config.pool_dir / wt_id


def resolve_wt_id_or_path(config: Config, value: str) -> tuple[str, Path]:
    if re.fullmatch(r"wt_[0-9]+", value):
        wt_id = value
        state = read_state(config, wt_id)
        path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).expanduser().resolve()
        return wt_id, path
    path = Path(value).expanduser().resolve()
    # Main worktree (repo root) accepted for sessions list/delete.
    if path == config.repo:
        return "xidi-minimal", path
    wt_id = path.name
    validate_wt_id(wt_id)
    return wt_id, path


def is_git_worktree(path: Path) -> bool:
    return path.is_dir() and git(path, "rev-parse", "--is-inside-work-tree", capture=True, check=False).returncode == 0


def worktree_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel", capture=True).stdout.strip()).resolve()


def assert_worktree_root(path: Path) -> None:
    if not is_git_worktree(path):
        fail(f"not a git worktree: {path}")
    root = worktree_root(path)
    if root != path:
        fail(f"worktree path must be root: got={path} root={root}")


def is_clean_worktree(path: Path) -> bool:
    return git(path, "status", "--porcelain", capture=True).stdout.strip() == ""


def branch_exists(repo: Path, branch: str) -> bool:
    return (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            capture=True,
            check=False,
        ).returncode
        == 0
    )


def remote_branch_exists(repo: Path, remote_branch: str) -> bool:
    return (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/{remote_branch}",
            capture=True,
            check=False,
        ).returncode
        == 0
    )


def validate_new_branch(repo: Path, branch: str, *, allow_existing: bool = False) -> None:
    if git(repo, "check-ref-format", "--branch", branch, capture=True, check=False).returncode != 0:
        fail(f"invalid branch name: {branch}")
    if not allow_existing and branch_exists(repo, branch):
        fail(f"local branch already exists: {branch}")
    if not allow_existing and remote_branch_exists(repo, f"origin/{branch}"):
        fail(f"remote branch already exists: origin/{branch}")


def ensure_base_ref(repo: Path, base_ref: str) -> None:
    if "/" in base_ref:
        remote, branch = base_ref.split("/", 1)
        eprint(f"fetch base ref: {remote} {branch}")
        git(repo, "fetch", remote, branch)
    if git(repo, "rev-parse", "--verify", base_ref, capture=True, check=False).returncode != 0:
        fail(f"base ref not found: {base_ref}")


def reset_to_base(path: Path, base_ref: str) -> None:
    git(path, "checkout", "--detach", base_ref)
    git(path, "reset", "--hard", base_ref)
    git(path, "clean", "-fd")


def checkout_task_branch(path: Path, repo: Path, branch: str, base_ref: str, *, allow_existing: bool) -> None:
    validate_new_branch(repo, branch, allow_existing=allow_existing)
    reset_to_base(path, base_ref)
    if allow_existing and branch_exists(repo, branch):
        git(path, "checkout", branch)
        git(path, "reset", "--hard", base_ref)
    else:
        git(path, "checkout", "-b", branch, "--no-track", base_ref)


# ---------------- OpenCode helpers ----------------


def opencode_config(config: Config) -> dict[str, Any]:
    path = config.repo / ".opencode" / "opencode.json"
    if not path.exists():
        fail(f"{path} not found")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def opencode_model(config: Config, agent: str) -> tuple[str, str, str | None]:
    data = opencode_config(config)
    agent_config = data.get("agent", {}).get(agent, {})
    variant = None
    if isinstance(agent_config, str):
        model_str = agent_config
    elif isinstance(agent_config, dict):
        model_str = agent_config.get("model") or data.get("small_model") or "opencode/deepseek-v4-flash-free"
        variant = agent_config.get("variant")
    else:
        model_str = data.get("small_model") or "opencode/deepseek-v4-flash-free"
    if "/" in model_str:
        provider_id, model_id = model_str.split("/", 1)
    else:
        provider_id, model_id = "opencode", model_str
    return provider_id, model_id, variant


def session_create_model(provider_id: str, model_id: str, variant: str | None) -> dict[str, str]:
    result = {"id": model_id, "providerID": provider_id}
    if variant:
        result["variant"] = variant
    return result


def prompt_model(provider_id: str, model_id: str) -> dict[str, str]:
    return {"providerID": provider_id, "modelID": model_id}


def sessions(config: Config, *, directory: Path | None = None, search: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query: dict[str, str | int] = {"limit": limit}
    if directory is not None:
        query["directory"] = str(directory)
    if search:
        query["search"] = search
    url = f"{config.op_server}/session?{urllib.parse.urlencode(query)}"
    data = http_json("GET", url)
    if not isinstance(data, list):
        fail("unexpected /session response")
    return cast(list[dict[str, Any]], data)


def get_session_by_id(config: Config, session_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    for session in sessions(config, directory=directory, limit=500):
        if session.get("id") == session_id:
            return session
    return None


def find_session_by_title(config: Config, title: str, directory: Path) -> dict[str, Any] | None:
    found = [
        item
        for item in sessions(config, directory=directory, search=title, limit=50)
        if item.get("title") == title and item.get("directory") and normalize_path(str(item["directory"])) == normalize_path(directory)
    ]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        found.sort(key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)
        latest = found[0]
        eprint(f"multiple sessions for {title}: using latest {latest.get('id')} (total {len(found)})")
        return latest
    return None


def watch_session(config: Config, session_id: str) -> None:
    http_json("POST", f"{config.sidecar}/watch", {"sessionID": session_id})


def unwatch_session(config: Config, session_id: str) -> None:
    try:
        http_json(
            "DELETE",
            f"{config.sidecar}/watch/{urllib.parse.quote(session_id)}",
            expected=(200, 404),
        )
    except SystemExit:
        pass


def _kill_idle_watch(config: Config, session_id: str) -> None:
    """Kill the idle-watch process watching ``session_id``, if any.

    Reads the PID file written by ``_spawn_dispatch_idle_watch`` and sends
    SIGTERM.  Best-effort — failures are logged but not fatal.
    """
    pid_file_path = _idle_pidfile(config, session_id)
    if not pid_file_path.exists():
        return
    try:
        pid = int(pid_file_path.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        pid_file_path.unlink(missing_ok=True)
    except (ValueError, ProcessLookupError, FileNotFoundError):
        pid_file_path.unlink(missing_ok=True)
    except Exception:
        pass


def delete_session(config: Config, session_id: str, *, hard: bool = False) -> None:
    """Unwatch session via sidecar. If ``hard``, also DELETE from OpenCode server.

    Default (hard=False) is safe: session remains in OpenCode for later
    inspection / recovery. Internal cleanup (release / ensure_session) always
    uses hard=False. Only user-facing ``sessions delete --hard --yes`` triggers
    permanent deletion.

    Also kills any idle-watch process watching this session to prevent orphan
    watchers.
    """
    _kill_idle_watch(config, session_id)
    unwatch_session(config, session_id)
    if hard:
        try:
            http_json(
                "DELETE",
                f"{config.op_server}/session/{urllib.parse.quote(session_id)}",
                expected=(200, 204, 404),
            )
        except SystemExit:
            pass


STALE_SESSION_MS_DEFAULT = 24 * 60 * 60 * 1000  # 1 day
STALE_DISPATCH_MS = 15 * 60 * 1000  # 15 min — auto-recover stuck dispatch sessions
MAX_MAIN_SESSION_CONTEXT = 200_000  # rebuild main agent session when context exceeds this

# Default recent window for `cmd_overview` and `rewatch_all_sessions`.
# Sessions whose `time.updated` is older than this are filtered out unless
# no recent session exists for that (wt_id, agent) pair, in which case the
# most-recent stale session is retained as a "last-known tombstone".
_OVERVIEW_RECENT_DEFAULT_SECONDS = 3 * 86400  # 3 days
_OVERVIEW_RECENT_DEFAULT_MS = _OVERVIEW_RECENT_DEFAULT_SECONDS * 1000

# Overview/sidecar should not scan an unbounded number of historical PM
# conversations from <pool_dir>/.state/sessions/<pm_session_id>/main.state.
# Keep the current PM session (if present) plus the newest historical PM states.
_PM_STATE_HISTORY_LIMIT_DEFAULT = 1


def is_session_stale(session: dict[str, Any], max_age_ms: int = STALE_SESSION_MS_DEFAULT) -> bool:
    """Return True if session's ``time.updated`` is older than ``max_age_ms`` (default 1 day)."""
    if not session:
        return False
    updated = int((session.get("time") or {}).get("updated") or 0)
    if not updated:
        return False
    return (int(time.time() * 1000) - updated) > max_age_ms


def normalize_agent_label(agent: object) -> str:
    """Return a stable display/grouping label for agent names.

    OpenCode/plugin sources may report the PM agent as either ``PM`` or
    ``pm``.  Overview and sidecar grouping must treat both as the same agent;
    other agent names preserve their original spelling.
    """
    if not isinstance(agent, str) or not agent:
        return "__unknown__"
    return "PM" if agent.lower() == "pm" else agent


def _is_pm_agent(agent: object) -> bool:
    return isinstance(agent, str) and agent.lower() == "pm"


def _pm_state_mtime_ms(state_file: Path) -> int:
    try:
        return int(state_file.stat().st_mtime_ns // 1_000_000)
    except OSError:
        return 0


def recent_pm_state_files(
    config: Config,
    *,
    limit: int = _PM_STATE_HISTORY_LIMIT_DEFAULT,
) -> list[tuple[str, Path, bool]]:
    """Return current + recent historical per-PM ``main.state`` files.

    ``limit`` applies only to historical PM sessions.  The active PM session
    (when it has a state file) is pinned and does not consume the historical
    budget.  This keeps overview/sidecar from resurrecting every old PM
    conversation while still showing the current PM plus the newest historical
    PM states from ``.state/sessions/<pm_session_id>/main.state``.
    """
    if limit < 0:
        fail("PM state history limit must be >= 0")
    sessions_root = state_dir(config) / "sessions"
    if not sessions_root.is_dir():
        return []

    current = config.pm_session_id
    rows: list[tuple[str, Path, bool, int]] = []
    for pm_dir in sorted(sessions_root.iterdir()):
        if not pm_dir.is_dir():
            continue
        state_file = pm_dir / "main.state"
        if not state_file.exists():
            continue
        pm_sid = pm_dir.name
        if not pm_sid.startswith("ses"):
            continue
        is_current = bool(pm_sid and pm_sid == current)
        rows.append((pm_sid, state_file, is_current, _pm_state_mtime_ms(state_file)))

    current_rows = [row for row in rows if row[2]]
    historical_rows = [row for row in rows if not row[2]]
    current_rows.sort(key=lambda item: (-item[3], item[0]))
    historical_rows.sort(key=lambda item: (-item[3], item[0]))
    selected = current_rows[:1] + historical_rows[:limit]
    return [(pm_sid, state_file, is_current) for pm_sid, state_file, is_current, _ in selected]


def tag_pm_session_ownership(config: Config, indexed: list[dict[str, Any]]) -> None:
    """Mutate main-worktree session index rows with PM owner metadata.

    Ownership comes from recent ``.state/sessions/<pm_sid>/main.state`` files.
    The PM session itself is also recognized by the state directory name, so
    historical PM conversations show as bounded PM groups instead of orphans.
    """
    pm_map = build_pm_session_map(config)
    owning_pm_sids = {pm_sid for pm_sid, _ in pm_map.values() if pm_sid}
    for it in indexed:
        raw = it.get("_raw")
        sid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
        pm_sid, is_current = pm_map.get(sid, ("", False))
        if not pm_sid and _is_pm_agent(it.get("agent")) and sid == config.pm_session_id:
            pm_sid = config.pm_session_id
            is_current = True
        elif not pm_sid and sid in owning_pm_sids:
            pm_sid = sid
            is_current = sid == config.pm_session_id
        it["pm_session_id"] = pm_sid
        it["pm_current"] = is_current


def cleanup_stale_sessions(
    config: Config,
    wt_id: str,
    max_age_ms: int = STALE_SESSION_MS_DEFAULT,
) -> list[tuple[str, str]]:
    """Archive (evict the state pointer, leave the session in OpenCode) any
    ``*_session_id`` field whose underlying session's ``time.updated`` exceeds
    ``max_age_ms``.

    The OpenCode session itself is NOT deleted — only its
    ``*_session_id``/``*_session_title`` entries in the state file are
    removed, so the next ``pool prepare``/``pool repair`` creates a fresh
    session with cold cache. The old session remains in OpenCode for
    history/inspection and is still visible via ``sessions list``.

    Used by ``release`` to keep the worktree's session pool fresh and
    bounded. Pairs with ``ensure_pool_sessions`` at prepare time: release
    evicts old, prepare re-creates.

    Returns the list of ``(agent, session_id)`` tuples whose state pointers
    were archived. Also prunes the ``deleted_session_ids`` tombstone field
    for any soft-deleted sid that no longer exists in OpenCode, so the
    tombstone set does not grow unbounded.
    """
    state = read_state(config, wt_id)
    cleaned: list[tuple[str, str]] = []
    alive_checked: set[str] = set()
    for key in list(state.keys()):
        if not key.endswith("_session_id"):
            continue
        agent = key[: -len("_session_id")]
        sid = state[key]
        if not sid:
            state.pop(key, None)
            state.pop(f"{agent}_session_title", None)
            continue
        try:
            ses = http_json(
                "GET",
                f"{config.op_server}/session/{urllib.parse.quote(sid)}",
            )
        except SystemExit:
            continue
        if not isinstance(ses, dict):
            continue
        alive_checked.add(sid)
        if is_session_stale(ses, max_age_ms):
            state.pop(key, None)
            state.pop(f"{agent}_session_title", None)
            cleaned.append((agent, sid))
    # Prune tombstoned sids that are no longer present in OpenCode, so the
    # tombstone field does not grow unbounded across many ``sessions delete``
    # invocations. Best-effort — failures are silently dropped (the next
    # cleanup cycle retries).
    pruned = False
    tombstoned = _tombstoned_sids(state)
    if tombstoned:
        alive: set[str] = set(alive_checked)
        for sid in tombstoned - alive_checked:
            try:
                ses = http_json(
                    "GET",
                    f"{config.op_server}/session/{urllib.parse.quote(sid)}",
                )
            except SystemExit:
                continue
            if isinstance(ses, dict):
                alive.add(sid)
        keep = tombstoned & alive
        if keep != tombstoned:
            state["deleted_session_ids"] = ",".join(sorted(keep)) if keep else ""
            pruned = True
    if cleaned or pruned:
        write_state(config, wt_id, state)
    return cleaned


def create_session(config: Config, wt_id: str, wt_path: Path, agent: str) -> dict[str, Any]:
    title = f"{wt_id}-{agent}"
    provider_id, model_id, variant = opencode_model(config, agent)
    body = {
        "title": title,
        "agent": agent,
        "model": session_create_model(provider_id, model_id, variant),
        "metadata": {
            "managedBy": "session-worktree-mgr.py",
            "wt_id": wt_id,
            "wt_path": str(wt_path),
            "agent": agent,
        },
    }
    query = urllib.parse.urlencode({"directory": str(wt_path)})
    data = http_json("POST", f"{config.op_server}/session?{query}", body, expected=(200, 201))
    if not isinstance(data, dict) or not data.get("id"):
        fail(f"create session failed: response missing id for {title}")
    return cast(dict[str, Any], data)


def ensure_session(
    config: Config,
    wt_id: str,
    wt_path: Path,
    agent: str,
    *,
    recreate_missing: bool = True,
    recreate_stale: bool = False,
    recreate_existing: bool = False,
    max_age_ms: int = STALE_SESSION_MS_DEFAULT,
) -> dict[str, Any]:
    """Return a usable session for ``(wt_id, agent)``.

    Lookup order:
      1. state ``*_session_id`` + ``get_session_by_id`` (still in OpenCode).
         The fast path also honors ``state['deleted_session_ids']``: if the
         pinned sid was soft-deleted via ``sessions delete``, the state
         pointer is bypassed and we fall through to step 2 — the user
         explicitly tombstoned it and we must not resurrect it.
      2. fallback: ``find_session_by_title`` (e.g. state was lost), skipping
         any soft-deleted sids in ``state['deleted_session_ids']`` (P1-5 —
         the user explicitly tombstoned these via ``sessions delete``, so
         we must not resurrect them as the "current" session)
      3. create new session via ``create_session``

    With ``recreate_stale=True``, sessions whose ``time.updated`` exceeds
    ``max_age_ms`` are ARCHIVED (unwatched, left in OpenCode for later
    inspection — cache is cold, conversation history is no longer useful)
    and a fresh session is created instead. This is the "grab-time
    refresh" behavior used by ``ensure_pool_sessions``.

    With ``recreate_existing=True``, ANY existing session (state-pinned or
    title-found) is archived first (unwatched, left in OpenCode) and a
    fresh session is created. This is the "always-fresh" behavior used by
    ``cmd_prepare`` to guarantee that every new task gets a brand-new
    session (no cross-task reuse), while repair / init / per-dispatch
    loops still preserve session continuity.
    """
    title = f"{wt_id}-{agent}"
    state = read_state(config, wt_id)
    tombstoned = _tombstoned_sids(state)
    state_sid = state.get(f"{agent}_session_id")
    if state_sid and state_sid in tombstoned:
        # Soft-deleted via ``sessions delete`` (no ``--hard``): the state
        # pointer still points to a tombstoned sid. Honor the tombstone
        # before consulting OpenCode so the fast path cannot resurrect a
        # session the user explicitly marked as deleted. Fall through to
        # ``find_session_by_title`` / ``create_session`` for a clean one.
        eprint(f"tombstoned state session skipped: {state_sid}")
    elif state_sid:
        item = get_session_by_id(config, state_sid, directory=wt_path)
        if item:
            if recreate_existing:
                eprint(f"old state session archived (not deleted): {state_sid}")
            elif recreate_stale and is_session_stale(item, max_age_ms):
                eprint(f"stale state session archived (> {max_age_ms}ms): {state_sid}")
                delete_session(config, state_sid)
            else:
                return item
        else:
            eprint(f"stale state session id archived: {agent} {state_sid}")
    item = find_session_by_title(config, title, wt_path)
    if item and str(item.get("id", "")) in tombstoned:
        # Soft-deleted via ``sessions delete`` (no ``--hard``): the title
        # match would resurrect a session the user explicitly tombstoned.
        # Fall through to create a fresh one so the next dispatch gets a
        # clean context window.
        eprint(f"tombstoned title match skipped: {item.get('id')}")
        item = None
    if item:
        if recreate_existing:
            eprint(f"session archived by title (not deleted): {item.get('id')}")
        elif recreate_stale and is_session_stale(item, max_age_ms):
            eprint(f"session archived by title (> {max_age_ms}ms): {item.get('id')}")
            delete_session(config, item["id"])
        else:
            return item
    if not recreate_missing:
        fail(f"missing session for {title}; run pool repair {wt_id}")
    return create_session(config, wt_id, wt_path, agent)


def persist_session(config: Config, wt_id: str, agent: str, session: dict[str, Any]) -> None:
    sid = str(session.get("id") or "")
    if not sid:
        fail(f"session missing id for {wt_id}-{agent}")
    update_state(
        config,
        wt_id,
        {
            f"{agent}_session_id": sid,
            f"{agent}_session_title": str(session.get("title") or f"{wt_id}-{agent}"),
        },
    )
    watch_session(config, sid)


# ---------------- node_modules copy ----------------


def copy_opencode_node_modules(config: Config, wt_path: Path, *, force: bool = False) -> None:
    source = config.repo / ".opencode" / "node_modules"
    target_dir = wt_path / ".opencode"
    target = target_dir / "node_modules"
    if not source.is_dir():
        fail(f"main .opencode/node_modules not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if not force:
            fail(f"worktree .opencode/node_modules is symlink; rerun with --force-copy to replace: {target}")
        target.unlink()
    if target.exists():
        if not force:
            eprint(f".opencode/node_modules already exists, keep: {target}")
            return
        shutil.rmtree(target)
    eprint(f"copy .opencode/node_modules: {source} -> {target}")
    shutil.copytree(source, target, symlinks=True)
    eprint(f"copied .opencode/node_modules: {target}")


# ---------------- service ----------------


def op_healthy(config: Config) -> bool:
    return http_code(f"{config.op_server}/global/health") == 200 or http_code(f"{config.op_server}/session") == 200


def sidecar_healthy(config: Config) -> bool:
    try:
        data = http_json("GET", f"{config.sidecar}/health")
        return isinstance(data, dict) and data.get("opencodeServer") == config.op_server
    except SystemExit:
        return False


def check_services(config: Config) -> None:
    if not op_healthy(config):
        fail(f"OpenCode server not healthy: {config.op_server}")
    if not sidecar_healthy(config):
        fail(f"sidecar not healthy: {config.sidecar}")


def pid_file(config: Config, name: str) -> Path:
    return config.log_dir / f"{name}.pid"


def log_file(config: Config, name: str) -> Path:
    return config.log_dir / f"{name}.log"


def read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def pid_alive(pid: int | None) -> TypeGuard[int]:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_until(fn: Callable[[], bool], timeout: int = 20) -> bool:
    for _ in range(timeout):
        if fn():
            return True
        time.sleep(1)
    return False


def rewatch_all_sessions(config: Config) -> None:
    """Re-register persisted session IDs with the sidecar after a sidecar restart.

    Scans every ``wt_*.state`` file in the pool directory, extracts each
    ``*_session_id`` field, fetches the session metadata from OpenCode to learn
    its ``agent`` + ``updated_ms``, and applies the same recent-window filter
    used by ``cmd_overview`` (see ``_apply_recent_filter``).  Sessions that
    no longer exist in OpenCode, or whose ``GET /session/{id}`` call fails,
    are skipped silently — they will be reconciled by the next ``pool prepare``
    or ``pool repair`` cycle.

    Sharing the filter with ``collect_overview`` keeps the sidecar's watch
    table aligned with what overview displays: a session the user explicitly
    removed via ``sessions delete`` (and therefore intentionally aged out of
    the recent window) stays unwatched even after a restart.
    """
    sd = state_dir(config)
    if not sd.is_dir():
        return
    candidates: list[dict[str, Any]] = []

    def _ingest_sid(sid: str, wt_id: str, pm_session_id: str = "") -> None:
        """Fetch session metadata and append to candidates.

        ``pm_session_id`` tags the candidate with its owning PM session so
        per-PM isolation flows through ``_apply_recent_filter``. Defaults to
        the empty string (back-compat for callers that don't track PM).
        """
        try:
            ses = http_json(
                "GET",
                f"{config.op_server}/session/{urllib.parse.quote(sid)}",
            )
        except SystemExit:
            # Network blip / sidecar down / 5xx — skip this session; we
            # don't want a transient failure to abort the whole restart
            # recovery loop.
            return
        if not isinstance(ses, dict):
            return
        meta = ses.get("metadata") or {}
        agent = meta.get("agent") or ses.get("agent") or "__unknown__"
        updated_ms = int((ses.get("time") or {}).get("updated") or 0)
        candidates.append(
            {
                "_sid": sid,
                "wt_id": wt_id,
                "pm_session_id": pm_session_id,
                "agent": normalize_agent_label(agent),
                "updated_ms": updated_ms,
            }
        )

    # Scan worktree pool state files (per-PM tagging does not apply; PM
    # sessions are scoped to main worktree via sessions/*/main.state below).
    for sf in sorted(sd.glob("wt_*.state")):
        wt_id = sf.stem
        if not re.fullmatch(r"wt_[0-9]+", wt_id):
            continue
        state = read_state(config, wt_id)
        tombstoned = _tombstoned_sids(state)
        for key, sid in state.items():
            if not key.endswith("_session_id") or not isinstance(sid, str) or not sid:
                continue
            if sid in tombstoned:
                # Soft-deleted via ``sessions delete`` (no ``--hard``): skip
                # the sidecar re-watch so the user stays unwatched until a
                # future ``pool repair`` creates a fresh session.
                continue
            _ingest_sid(sid, wt_id)

    # Scan recent per-PM-session main.state files for main agents.  This is
    # intentionally capped (current PM + newest historical PM state by default)
    # so sidecar restart recovery does not re-watch every old PM conversation.
    # The PM session itself is also ingested from the state directory name so
    # sidecar can report current/old PM sessions instead of only their agents.
    for pm_sid, state_file, _is_current in recent_pm_state_files(config):
        main_state = _read_state_file(state_file)
        main_tombstoned = _tombstoned_sids(main_state)
        if pm_sid and pm_sid not in main_tombstoned:
            _ingest_sid(pm_sid, "xidi-minimal", pm_session_id=pm_sid)
        for key, sid in main_state.items():
            if key.endswith("_session_id") and sid and sid not in main_tombstoned:
                _ingest_sid(sid, "xidi-minimal", pm_session_id=pm_sid)
    filtered = _apply_recent_filter(
        candidates,
        now_ms=int(time.time() * 1000),
        recent_seconds=_OVERVIEW_RECENT_DEFAULT_SECONDS,
        pm_session_id="pm_session_id",
    )
    rewired = 0
    for fs in filtered:
        try:
            watch_session(config, fs["_sid"])
            rewired += 1
        except SystemExit:
            pass
    if rewired:
        eprint(f"rewired {rewired} session watch(es) after sidecar (re)start")


def cmd_opencode_serve_service(args: argparse.Namespace, config: Config) -> None:
    """管理 OpenCode Server 进程（opencode serve）。

    独立管理，不依赖 Sidecar。start/stop/status/restart 仅作用于 OpenCode Server。
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    op_pid_file = pid_file(config, "opencode-server")
    if args.action == "status":
        print(
            json.dumps(
                {
                    "service": "opencode-server",
                    "healthy": op_healthy(config),
                    "pid": read_pid(op_pid_file),
                    "pidFile": str(op_pid_file),
                    "logFile": str(log_file(config, "opencode-server")),
                    "url": config.op_server,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.action in ("stop", "restart"):
        pid = read_pid(op_pid_file)
        if pid_alive(pid):
            eprint(f"opencode-server: stop pid={pid}")
            os.kill(pid, signal.SIGTERM)
            for _ in range(5):
                if not pid_alive(pid):
                    break
                time.sleep(1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        op_pid_file.unlink(missing_ok=True)
        if args.action == "stop":
            return
    if args.action in ("start", "restart"):
        require_cmd("opencode")
        if not op_healthy(config):
            op_log = open(log_file(config, "opencode-server"), "ab")
            proc = subprocess.Popen(
                ["opencode", "serve", "--hostname", config.op_host, "--port", str(config.op_port)],
                cwd=str(config.repo),
                stdout=op_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            op_pid_file.write_text(str(proc.pid), encoding="utf-8")
            if not wait_until(lambda: op_healthy(config)):
                fail(f"OpenCode Server did not become healthy; log={log_file(config, 'opencode-server')}")
            eprint(f"OpenCode Server: started pid={proc.pid}")
        else:
            eprint("OpenCode Server: already healthy")


def cmd_sidecar_service(args: argparse.Namespace, config: Config) -> None:
    """管理 Session Status Sidecar 进程（node scripts/session-status-server.mjs）。

    独立管理，不依赖 OpenCode Server。start/restart 后自动恢复所有持久化 session watch。
    """
    config.log_dir.mkdir(parents=True, exist_ok=True)
    sidecar_pid_file = pid_file(config, "session-status-server")
    if args.action == "status":
        print(
            json.dumps(
                {
                    "service": "session-status-server",
                    "healthy": sidecar_healthy(config),
                    "pid": read_pid(sidecar_pid_file),
                    "pidFile": str(sidecar_pid_file),
                    "logFile": str(log_file(config, "session-status-server")),
                    "url": config.sidecar,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.action in ("stop", "restart"):
        pid = read_pid(sidecar_pid_file)
        if pid_alive(pid):
            eprint(f"session-status-server: stop pid={pid}")
            os.kill(pid, signal.SIGTERM)
            for _ in range(5):
                if not pid_alive(pid):
                    break
                time.sleep(1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        sidecar_pid_file.unlink(missing_ok=True)
        if args.action == "stop":
            return
    if args.action in ("start", "restart"):
        require_cmd("node")
        sidecar_script = config.repo / "scripts" / "session-status-server.mjs"
        if not sidecar_script.exists():
            fail(f"sidecar script not found: {sidecar_script}")
        if not sidecar_healthy(config):
            sidecar_log = open(log_file(config, "session-status-server"), "ab")
            proc = subprocess.Popen(
                ["node", str(sidecar_script)],
                cwd=str(config.repo),
                env={
                    **os.environ,
                    "OPENCODE_SERVER": config.op_server,
                    "SESSION_STATUS_HOST": config.sidecar_host,
                    "SESSION_STATUS_PORT": str(config.sidecar_port),
                },
                stdout=sidecar_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            sidecar_pid_file.write_text(str(proc.pid), encoding="utf-8")
            if not wait_until(lambda: sidecar_healthy(config)):
                fail(f"Status Sidecar did not become healthy; log={log_file(config, 'session-status-server')}")
            eprint(f"Status Sidecar: started pid={proc.pid}")
        else:
            eprint("Status Sidecar: already healthy")
        # Sidecar 重启后 watch 表为空，恢复所有持久化的 session watch
        rewatch_all_sessions(config)


# ---------------- pool ----------------


def create_or_reuse_worktree(config: Config, wt_id: str) -> Path:
    wt_path = path_for_wt(config, wt_id)
    config.pool_dir.mkdir(parents=True, exist_ok=True)
    ensure_base_ref(config.repo, config.base_ref)
    if not wt_path.exists():
        eprint(f"create worktree: {wt_id} {wt_path}")
        git(config.repo, "worktree", "add", "--detach", str(wt_path), config.base_ref)
    else:
        eprint(f"reuse worktree: {wt_id} {wt_path}")
    assert_worktree_root(wt_path)
    return wt_path


def repair_one(config: Config, wt_id: str, agents: list[str], *, reset: bool, force_copy: bool) -> dict[str, Any]:
    validate_wt_id(wt_id)
    wt_path = create_or_reuse_worktree(config, wt_id)
    if reset:
        reset_to_base(wt_path, config.base_ref)
    copy_opencode_node_modules(config, wt_path, force=force_copy)
    state = read_state(config, wt_id)
    state.update(
        {
            "status": state.get("status") or "idle",
            "wt_id": wt_id,
            "wt_path": str(wt_path),
            "base_ref": config.base_ref,
            "initialized": "1",
            "node_modules_mode": "copy",
            "updated_at": now_utc(),
        }
    )
    write_state(config, wt_id, state)
    session_results = []
    for agent in agents:
        session = ensure_session(config, wt_id, wt_path, agent, recreate_missing=True)
        persist_session(config, wt_id, agent, session)
        provider_id, model_id, variant = opencode_model(config, agent)
        model_label = f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")
        session_results.append(
            {
                "agent": agent,
                "sessionID": session.get("id"),
                "title": session.get("title"),
                "directory": session.get("directory"),
                "model": model_label,
            }
        )
    return {"wt_id": wt_id, "wt_path": str(wt_path), "sessions": session_results}


def validate_pool_sessions(config: Config, wt_id: str, agents: list[str]) -> None:
    state = read_state(config, wt_id)
    wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    if state.get("initialized") != "1":
        fail(f"{wt_id} is not initialized; run pool repair {wt_id}")
    if state.get("node_modules_mode") != "copy":
        fail(f"{wt_id} node_modules is not copy mode; run pool repair {wt_id} --force-copy")
    assert_worktree_root(wt_path)
    for agent in agents:
        sid = state.get(f"{agent}_session_id")
        if not sid:
            fail(f"{wt_id} missing {agent}_session_id; run pool repair {wt_id}")
        if not get_session_by_id(config, sid, directory=wt_path):
            fail(f"{wt_id} stale {agent}_session_id={sid}; run pool repair {wt_id}")


def ensure_pool_sessions(
    config: Config,
    wt_id: str,
    wt_path: Path,
    agents: list[str],
    *,
    recreate_stale: bool = True,
    recreate_always: bool = False,
) -> list[dict[str, Any]]:
    """Ensure each agent has a fresh, valid session — create if missing or stale.

    Companion to ``cleanup_stale_sessions`` (called by ``release``):
    release evicts old sessions; ensure_pool_sessions (called by ``prepare``)
    re-creates them so the wt is ready for dispatch.

    With ``recreate_stale=True`` (default), sessions older than
    ``STALE_SESSION_MS_DEFAULT`` (1 day) are deleted and replaced.

    With ``recreate_always=True`` (used by ``cmd_prepare``), every existing
    session is deleted and replaced — guarantees no cross-task session
    reuse, so a new task starts with a cold cache and zero history from
    previous tasks. Per-dispatch loops within the same task still reuse
    the session (dispatch itself does not call this function).
    """
    results: list[dict[str, Any]] = []
    for agent in agents:
        session = ensure_session(
            config,
            wt_id,
            wt_path,
            agent,
            recreate_missing=True,
            recreate_stale=recreate_stale,
            recreate_existing=recreate_always,
        )
        persist_session(config, wt_id, agent, session)
        results.append(session)
    return results


def cmd_pool_init(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    agents = parse_agents(args.agents)
    if args.size < 1:
        fail("--size must be >= 1")
    if args.size > config.max_worktrees:
        fail(f"--size exceeds MAX_WORKTREES={config.max_worktrees}")
    results = []
    with pool_lock(config):
        for i in range(1, args.size + 1):
            wt_id = wt_id_for_index(i)
            eprint(f"== pool init {wt_id} ==")
            results.append(repair_one(config, wt_id, agents, reset=args.reset, force_copy=args.force_copy))
    print(json.dumps({"pool_dir": str(config.pool_dir), "results": results}, ensure_ascii=False, indent=2))


def cmd_pool_repair(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    agents = parse_agents(args.agents)
    with pool_lock(config):
        result = repair_one(config, args.wt_id, agents, reset=args.reset, force_copy=args.force_copy)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def pool_row(config: Config, wt_id: str, agents: list[str], *, verify: bool) -> dict[str, Any]:
    state = read_state(config, wt_id)
    wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    row: dict[str, Any] = {
        "wt_id": wt_id,
        "status": state.get("status", "missing"),
        "wt_path": str(wt_path),
        "branch": state.get("branch", ""),
        "initialized": state.get("initialized", "0"),
        "node_modules_mode": state.get("node_modules_mode", ""),
        "sessions": {},
    }
    for agent in agents:
        sid = state.get(f"{agent}_session_id", "")
        exists = None
        if verify and sid:
            exists = get_session_by_id(config, sid, directory=wt_path) is not None
        row["sessions"][agent] = {"sessionID": sid, "exists": exists}
    return row


def cmd_pool_status(args: argparse.Namespace, config: Config) -> None:
    agents = parse_agents(args.agents)
    rows = [pool_row(config, wt_id_for_index(i), agents, verify=args.verify) for i in range(1, args.size + 1)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


# ---------------- prepare / dispatch / release ----------------

# Round-robin state for find_idle_wt(). Persisted to
# ``<pool_dir>/.state/.pool_rr_index`` so it survives across CLI invocations
# (each ``python3 ...`` is a new process). Missing or corrupt file resets
# to wt_1 (per PM convention 2026-06-08).
_RR_STATE_FILENAME = ".pool_rr_index"


def _read_rr_index(config: Config) -> int:
    path = state_dir(config) / _RR_STATE_FILENAME
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 1


def _write_rr_index(config: Config, idx: int) -> None:
    path = state_dir(config) / _RR_STATE_FILENAME
    try:
        state_dir(config).mkdir(parents=True, exist_ok=True)
        path.write_text(str(idx))
    except OSError:
        # Best-effort: if we cannot persist, the next call resets to wt_1.
        pass


def find_idle_wt(config: Config) -> tuple[str, Path]:
    """Pick the next idle worktree via round-robin (persists across CLI calls).

    Scans wt_1..wt_<pool_size> starting from the persisted ``_RR_STATE_FILENAME``
    index. On a hit, advances the persisted pointer to the slot after the
    picked one (wraps to wt_1 after wt_<pool_size>). Falls through to the
    original "no idle initialized worktree" failure if every slot is busy.
    """
    pool_size = config.pool_size
    start = _read_rr_index(config)
    if start < 1 or start > pool_size:
        start = 1
    for offset in range(pool_size):
        i = ((start - 1 + offset) % pool_size) + 1
        wt_id = wt_id_for_index(i)
        state = read_state(config, wt_id)
        if state.get("initialized") == "1" and state.get("status", "idle") == "idle":
            _write_rr_index(config, 1 if i >= pool_size else i + 1)
            return wt_id, Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
    fail("no idle initialized worktree; run pool status or pool init")
    raise AssertionError("unreachable")


def cmd_prepare(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    agents = parse_agents(args.agents)
    with pool_lock(config):
        wt_id, wt_path = find_idle_wt(config)
        if not is_clean_worktree(wt_path):
            fail(f"selected worktree is dirty: {wt_path}; run release --force or pool repair")
        ensure_base_ref(config.repo, config.base_ref)
        checkout_task_branch(wt_path, config.repo, args.branch, config.base_ref, allow_existing=args.force_branch)
        # Ensure each agent has a fresh, valid session. Pairs with the
        # cleanup_stale_sessions() call inside cmd_release: release evicts
        # stale (>1d) sessions, prepare re-creates them so the wt is ready.
        # recreate_always=True guarantees no cross-task session reuse — every
        # new prepare gets brand-new sessions (per-task isolation).
        ensure_pool_sessions(
            config,
            wt_id,
            wt_path,
            agents,
            recreate_stale=True,
            recreate_always=True,
        )
        update_state(config, wt_id, {"status": "busy", "branch": args.branch, "wt_path": str(wt_path)})
    print(f"wt_id={wt_id}")
    print(f"wt_path={wt_path}")
    print(f"branch={args.branch}")
    print()
    print("下一步：先预览 prompt，不会发送")
    print(f'{PROG} pool dispatch {wt_id} {agents[0]} --task "..."')
    print()
    print("用户确认后再发送")
    print(f'{PROG} pool dispatch {wt_id} {agents[0]} --task "..." --yes')
    print()
    print("（可选：加 --notify-session <PM_SESSION_ID> 自动 idle-watch，或设 $PM_SESSION_ID）")


def render_prompt(wt_dir: Path, task: str) -> str:
    return f"""Assigned worktree: {wt_dir}

任务：
{task}

执行约束：
- 按 workflow 完成任务
- 修改前先读目标文件和关联文件，不对路径、签名、契约做假设
- 遇到阻塞不绕行，立即报告 blocker 与原因
"""


def cmd_dispatch(args: argparse.Namespace, config: Config) -> None:
    check_services(config)
    if args.session:
        if args.wt_id:
            fail(
                "dispatch accepts either '--session ses_xxx' OR 'wt_N Agent', not both.\n"
                "Examples:\n"
                f'  {PROG} dispatch wt_1 Daedalus --task "..."\n'
                f'  {PROG} dispatch --session ses_xxx --task "..." --yes\n'
                f'  {PROG} session dispatch ses_xxx --task "..." --yes'
            )
    else:
        if not args.wt_id or not args.agent:
            fail(
                "dispatch requires either '--session ses_xxx' or positional 'wt_N Agent'.\n"
                "Examples:\n"
                f'  {PROG} dispatch wt_1 Daedalus --task "..."\n'
                f'  {PROG} dispatch --session ses_xxx --task "..." --yes\n'
                f'  {PROG} session dispatch ses_xxx --task "..." --yes'
            )
    if args.session:
        # Direct session dispatch — bypass wt_id/state lookup.
        # Used for main-repo agents (Janitor/General) that have persistent
        # sessions created by `sessions create`.
        sid = args.session
        ses = get_session_by_id(config, sid)
        if not ses:
            fail(f"session not found: {sid}")
        wt_path = Path(ses.get("directory") or str(config.repo)).resolve()
        wt_id = ses.get("metadata", {}).get("wt_id", "main")
        agent = args.agent or str(ses.get("metadata", {}).get("agent") or ses.get("agent") or "")
        if not agent:
            fail("--agent required when --session metadata has no agent")
        if not wt_path.is_dir():
            fail(f"session directory not found: {wt_path}")
    else:
        wt_id = args.wt_id
        validate_wt_id(wt_id)
        state = read_state(config, wt_id)
        wt_path = Path(state.get("wt_path") or path_for_wt(config, wt_id)).resolve()
        agent = args.agent
        sid = state.get(f"{agent}_session_id")
        if not sid:
            fail(f"{wt_id} missing {agent}_session_id; run pool repair {wt_id}")
        if not get_session_by_id(config, sid, directory=wt_path):
            fail(f"{wt_id} stale {agent}_session_id={sid}; run pool repair {wt_id}")
    watch_session(config, sid)
    status_map = http_json("GET", f"{config.sidecar}/status")
    session_status = "unknown"
    if isinstance(status_map, dict):
        session_status = str(status_map.get(sid, "unknown"))
    if args.require_no_busy and session_status not in ("idle", "unknown"):
        fail(f"session {sid} not dispatchable: {session_status} (--require-no-busy)")
    if session_status in ("busy", "streaming"):
        force_recover = getattr(args, "force", False)
        if args.session:
            # --session dispatch doesn't support forced recovery (named session)
            if force_recover:
                fail("--force not supported for --session dispatch; use pool dispatch wt_N Agent --force instead")
            fail(f"session {sid} is not dispatchable: {session_status}")
        if not force_recover:
            # Auto-recover if session has been stuck > 15 min
            ses = get_session_by_id(config, sid, directory=wt_path)
            if ses and is_session_stale(ses, max_age_ms=STALE_DISPATCH_MS):
                force_recover = True
                eprint(f"auto-recovering stuck session {sid} (busy > {STALE_DISPATCH_MS // 60000}min)")
        if force_recover:
            delete_session(config, sid, hard=True)
            update_state(config, wt_id, {f"{agent}_session_id": ""})
            ses = ensure_session(config, wt_id, wt_path, agent, recreate_existing=True)
            sid = ses["id"]
            persist_session(config, wt_id, agent, ses)
            watch_session(config, sid)
            status_map = http_json("GET", f"{config.sidecar}/status")
            session_status = "unknown"
            if isinstance(status_map, dict):
                session_status = str(status_map.get(sid, "unknown"))
        else:
            fail(f"session {sid} is not dispatchable: {session_status} (use --force to hard-delete stuck session)")
    provider_id, model_id, variant = opencode_model(config, agent)
    prompt = render_prompt(wt_path, args.task.strip())
    body: dict[str, Any] = {
        "agent": agent,
        "model": prompt_model(provider_id, model_id),
        "parts": [{"type": "text", "text": prompt}],
    }
    if variant:
        body["variant"] = variant
    model_label = f"{provider_id}/{model_id}" + (f":{variant}" if variant else "")
    preview = {
        "send": bool(args.yes),
        "wt_id": wt_id,
        "agent": agent,
        "sessionID": sid,
        "status": session_status,
        "model": model_label,
        "directory": str(wt_path),
        "prompt": prompt,
    }
    if not args.yes:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print()
        print("确认后执行：")
        notify_flag = f" --notify-session {args.notify_session}" if args.notify_session else ""
        if args.session:
            # Direct session dispatch — no wt_id/agent positional args
            print(f"python3 scripts/session-worktree-mgr.py dispatch --session {sid} --task {json.dumps(args.task, ensure_ascii=False)} --yes{notify_flag}")
        else:
            print(f"python3 scripts/session-worktree-mgr.py dispatch {args.wt_id} {agent} --task {json.dumps(args.task, ensure_ascii=False)} --yes{notify_flag}")
        return
    query = urllib.parse.urlencode({"directory": str(wt_path)})
    url = f"{config.op_server}/session/{sid}/prompt_async?{query}"
    # Capture the dispatch timestamp before prompt_async so the auto idle-watch
    # can detect very short tasks that finish before the watcher observes
    # busy/streaming.  The fallback only fires when an assistant reply appears
    # after this timestamp, so it does not reintroduce initial-idle false
    # positives.
    dispatch_started_at_ms = int(time.time() * 1000)
    http_json("POST", url, body, expected=(204,))
    if args.session:
        persist_main_session(config, agent, ses)
    disp_label = f"{wt_id}-{agent}" if not args.session else f"main-{agent}"
    print(f"dispatched -> {disp_label} ({sid}) status=accepted model={model_label} directory={wt_path}")
    notify_sid = args.notify_session or config.pm_session_id
    if notify_sid:
        _idle_validate_ses("--notify-session", notify_sid)
        _spawn_dispatch_idle_watch(
            config,
            sid,
            notify_sid,
            wt_path,
            max_poll_seconds=args.max_poll_seconds,
            started_at_ms=dispatch_started_at_ms,
        )


def cmd_release(args: argparse.Namespace, config: Config) -> None:
    wt_id, wt_path = resolve_wt_id_or_path(config, args.target)
    assert_worktree_root(wt_path)
    with pool_lock(config):
        if not is_clean_worktree(wt_path):
            if not args.force:
                status = git(wt_path, "status", "--porcelain", capture=True).stdout
                eprint(status)
                fail("worktree is dirty. Commit/stash changes or use --force")
            git(wt_path, "reset", "--hard")
            git(wt_path, "clean", "-fd")
        ensure_base_ref(wt_path, config.base_ref)
        reset_to_base(wt_path, config.base_ref)
        # Evict sessions older than STALE_SESSION_MS_DEFAULT (1 day) so the
        # next prepare creates a fresh session pool instead of reusing cold cache.
        cleaned = cleanup_stale_sessions(config, wt_id)
        update_state(
            config,
            wt_id,
            {"status": "idle", "branch": "", "wt_path": str(wt_path), "base_ref": config.base_ref},
        )
    print(f"wt_id={wt_id}")
    print(f"wt_path={wt_path}")
    print("status=idle")
    if cleaned:
        for agent, sid in cleaned:
            # The OpenCode session is NOT deleted — only the state pointer
            # is evicted. The session remains in OpenCode for history.
            print(f"evicted from state (session left in OpenCode): {agent} {sid}")


# ---------------- sessions management ----------------


def session_items_for_wt(config: Config, wt_id: str, wt_path: Path, agents: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Main worktree sessions use "main" as wt_id in titles, not the repo dir name.
    title_prefix = "main" if wt_id == "xidi-minimal" else wt_id
    for agent in agents:
        title = f"{title_prefix}-{agent}"
        for session in sessions(config, directory=wt_path, search=title, limit=100):
            if session.get("title") == title:
                item = dict(session)
                item["_agent"] = agent
                items.append(item)
    return items


def session_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("updatedAt") or item.get("time", {}).get("updated") or item.get("id") or "")


def cmd_session_create(args: argparse.Namespace, config: Config) -> None:
    """Create a persistent session for a main-repo agent (Janitor/General/Momus/Clio).

    These agents work in the main repository directory (not in a worktree),
    and their sessions outlive the prepare→dispatch→release cycle.  Session
    ID is persisted per PM session in ``<pool_dir>/.state/sessions/<pm_sid>/main.state``
    so that each PM conversation gets its own set of main agents — no
    cross-conversation context leak.

    By default idempotent: returns existing non-stale session if one exists.
    Sessions are rebuilt when context exceeds ``MAX_MAIN_SESSION_CONTEXT``
    (200K tokens) or age exceeds 1 day.  ``--force`` hard-deletes the existing
    session and creates a fresh one unconditionally.
    """
    check_services(config)
    agent = args.agent
    directory = Path(args.directory).expanduser().resolve() if args.directory else config.repo
    if not directory.is_dir():
        fail(f"directory not found: {directory}")
    title = f"main-{agent}"
    # If a session for this agent already exists in this PM session's state,
    # reuse it (create_session is idempotent in the sense that we only call it
    # when we don't already have a live one).  If the persisted session is stale
    # (>1d), recreate.
    main_state = read_main_state(config)
    existing_sid = main_state.get(f"{agent}_session_id")
    if existing_sid and not args.force:
        ses = get_session_by_id(config, existing_sid, directory=directory)
        if ses and not is_session_stale(ses):
            # Check context bloat — rebuild if context window exceeds threshold
            ctx = fetch_session_context(config, existing_sid)
            if ctx > MAX_MAIN_SESSION_CONTEXT:
                eprint(f"context exceeded ({ctx // 1000}K > {MAX_MAIN_SESSION_CONTEXT // 1000}K): rebuilding {agent} {existing_sid}")
                delete_session(config, existing_sid)
            else:
                print(json.dumps({"sessionID": existing_sid, "agent": agent, "title": title, "directory": str(directory), "status": "existing"}, ensure_ascii=False))
                return
        elif ses:
            eprint(f"stale session archived: {agent} {existing_sid}")
            delete_session(config, existing_sid)
    elif existing_sid and args.force:
        eprint(f"force: deleting existing session: {agent} {existing_sid}")
        delete_session(config, existing_sid, hard=True)
    session = create_session(config, "main", directory, agent)
    persist_main_session(config, agent, session)
    print(json.dumps({"sessionID": session.get("id"), "agent": agent, "title": title, "directory": str(directory), "status": "created"}, ensure_ascii=False))


def _main_state_file(config: Config) -> Path:
    """Return the path to the main-agent session state file.

    When a PM session is active (``config.pm_session_id`` non-empty), the file
    is scoped to that PM session so that each PM conversation gets its own set
    of main agents (Momus, Clio, Janitor, General).  Old global state
    (``.state/main.state``) is migrated to the per-session path on first access.
    """
    if config.pm_session_id:
        session_dir = state_dir(config) / "sessions" / config.pm_session_id
        new_path = session_dir / "main.state"
        old_path = state_dir(config) / "main.state"
        session_dir.mkdir(parents=True, exist_ok=True)
        if old_path.exists() and not new_path.exists():
            shutil.copy2(str(old_path), str(new_path))
        return new_path
    return state_dir(config) / "main.state"


def read_main_state(config: Config) -> dict[str, str]:
    path = _main_state_file(config)
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def build_pm_session_map(config: Config) -> dict[str, tuple[str, bool]]:
    """Return session_id -> (pm_session_id, is_current) for recent PM state.

    Only the bounded recent PM state set is scanned (current PM + newest
    historical PM state by default).  The PM session itself is mapped from the
    ``sessions/<pm_sid>`` directory name in addition to child main-agent session
    ids stored inside ``main.state``.
    """
    result: dict[str, tuple[str, bool]] = {}
    for pm_sid, state_file, is_current in recent_pm_state_files(config):
        if pm_sid:
            result[pm_sid] = (pm_sid, is_current)
        state = _read_state_file(state_file)
        tombstoned = _tombstoned_sids(state)
        for key, val in state.items():
            if key.endswith("_session_id") and val and val not in tombstoned:
                result[val] = (pm_sid, is_current)
    return result


def write_main_state(config: Config, values: dict[str, str]) -> None:
    state_dir(config).mkdir(parents=True, exist_ok=True)
    _main_state_file(config).write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def update_main_state(config: Config, patch: dict[str, str]) -> dict[str, str]:
    state = read_main_state(config)
    state.update(patch)
    write_main_state(config, state)
    return state


def persist_main_session(config: Config, agent: str, session: dict[str, Any]) -> None:
    sid = str(session.get("id") or "")
    if not sid:
        fail(f"session missing id for main-{agent}")
    update_main_state(
        config,
        {
            f"{agent}_session_id": sid,
            f"{agent}_session_title": str(session.get("title") or f"main-{agent}"),
        },
    )
    watch_session(config, sid)


def _warn_deprecated(message: str) -> None:
    eprint(f"DEPRECATED: {message}")


def _resolve_sessions_filter(args: argparse.Namespace, config: Config) -> tuple[str, Path]:
    """Resolve sessions list/delete target from explicit filters.

    Preferred interface:
      - --wt wt_N
      - --main
      - --path /abs/worktree

    Back-compat:
      - positional target (hidden in help) still works, but emits a deprecation warning.
    """
    selected = [bool(getattr(args, "wt", None)), bool(getattr(args, "main", False)), bool(getattr(args, "path", None))]
    if sum(selected) > 1:
        fail(
            f"sessions accepts exactly one target filter: --wt, --main, or --path.\nExamples:\n  {PROG} sessions list --wt wt_1\n  {PROG} sessions list --main\n  {PROG} sessions list --path /abs/path"
        )
    if getattr(args, "target", None):
        if any(selected):
            fail("do not combine legacy positional target with --wt/--main/--path")
        _warn_deprecated(f'use "sessions list --wt wt_N" or "sessions list --main" instead of positional target {args.target!r}.')
        return resolve_wt_id_or_path(config, args.target)
    if getattr(args, "main", False):
        return "xidi-minimal", config.repo
    if getattr(args, "wt", None):
        wt = args.wt
        if wt.isdigit():
            wt = f"wt_{wt}"
        return resolve_wt_id_or_path(config, wt)
    if getattr(args, "path", None):
        return resolve_wt_id_or_path(config, args.path)
    fail(
        "sessions list/delete requires one target filter: --wt wt_N, --main, or --path /abs/path.\n"
        "For one session by ID, use:\n"
        f"  {PROG} session show ses_xxx\n"
        f"  {PROG} session status ses_xxx\n"
        f"  {PROG} session last ses_xxx"
    )


def _state_files_for_all_sessions(config: Config) -> list[tuple[str, Path]]:
    """Return (scope_id, state_file_path) pairs for wt_N and per-PM main states."""
    sd = state_dir(config)
    out: list[tuple[str, Path]] = []
    if not sd.is_dir():
        return out
    for sf in sorted(sd.glob("wt_*.state")):
        wt_id = sf.stem
        if re.fullmatch(r"wt_[0-9]+", wt_id):
            out.append((wt_id, sf))
    sessions_root = sd / "sessions"
    if sessions_root.is_dir():
        for pm_dir in sorted(sessions_root.iterdir()):
            if not pm_dir.is_dir():
                continue
            sf = pm_dir / "main.state"
            if sf.exists():
                out.append(("xidi-minimal", sf))
    old_main = sd / "main.state"
    if old_main.exists():
        out.append(("xidi-minimal", old_main))
    return out


def _add_tombstone_by_session_id(config: Config, sid: str) -> list[str]:
    """Soft-delete by session id wherever it appears in persisted state."""
    touched: list[str] = []
    for scope, sf in _state_files_for_all_sessions(config):
        state = _read_state_file(sf)
        if not any(k.endswith("_session_id") and v == sid for k, v in state.items()):
            continue
        tombstoned = _tombstoned_sids(state)
        tombstoned.add(sid)
        state["deleted_session_ids"] = ",".join(sorted(tombstoned))
        _write_state_file(sf, state)
        touched.append(f"{scope}:{sf}")
    return touched


def _session_agent_label(session: dict[str, Any]) -> str:
    meta = session.get("metadata") or {}
    agent = meta.get("agent") or session.get("agent") or "__unknown__"
    return normalize_agent_label(agent)


def _sessions_for_filter(config: Config, wt_id: str, wt_path: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Return sessions for sessions list/delete.

    If --agent/--agents is supplied, use exact title-based lookup for those
    agents. If omitted, list every session belonging to the target worktree/main
    directory so AI does not need to infer the agent set.
    """
    if getattr(args, "agent", None) or getattr(args, "agents", None):
        agents = [args.agent] if args.agent else parse_agents(args.agents)
        return session_items_for_wt(config, wt_id, wt_path, agents)
    items: list[dict[str, Any]] = []
    for s in collect_wt_sessions(config, wt_id, str(wt_path)):
        item = dict(s)
        item["_agent"] = _session_agent_label(item)
        items.append(item)
    return items


def cmd_sessions_list(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        fail(
            '"sessions list" lists multiple sessions by filters and does not accept --session.\n'
            "For one session, use:\n"
            f"  {PROG} session show {args.session}\n"
            f"  {PROG} session status {args.session}\n"
            f"  {PROG} session last {args.session}"
        )
    wt_id, wt_path = _resolve_sessions_filter(args, config)
    items = _sessions_for_filter(config, wt_id, wt_path, args)
    items.sort(key=session_sort_key, reverse=True)
    if args.format == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for item in items:
        updated = item.get("updatedAt") or (item.get("time") or {}).get("updated", "")
        print(f"{item.get('id')}\t{item.get('title')}\t{item.get('directory')}\t{updated}")


def cmd_sessions_delete(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        fail(
            '"sessions delete" deletes multiple sessions by filters and does not accept --session.\n'
            "For one session, use:\n"
            f"  {PROG} session delete {args.session} --yes\n"
            f"  {PROG} session delete {args.session} --hard --yes"
        )
    wt_id, wt_path = _resolve_sessions_filter(args, config)
    items = _sessions_for_filter(config, wt_id, wt_path, args)
    items.sort(key=session_sort_key, reverse=True)
    if args.keep_latest:
        kept: set[str] = set()
        delete_list: list[dict[str, Any]] = []
        for item in items:
            agent = str(item.get("_agent"))
            if agent not in kept:
                kept.add(agent)
                continue
            delete_list.append(item)
    else:
        delete_list = items
    print(
        json.dumps(
            {
                "dryRun": not args.yes,
                "target": wt_id,
                "directory": str(wt_path),
                "mode": "hard-delete" if args.hard else "soft-delete/tombstone",
                "delete": [{"id": x.get("id"), "title": x.get("title"), "agent": x.get("_agent")} for x in delete_list],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not args.yes:
        print()
        print("确认删除后加 --yes")
        return
    for item in delete_list:
        sid = str(item.get("id") or "")
        if not sid:
            continue
        if not args.hard:
            _add_tombstone(config, wt_id, sid)
        delete_session(config, sid, hard=args.hard)
        eprint(f"{'tombstoned' if not args.hard else 'deleted'} session: {sid}{' (hard)' if args.hard else ''}")


# ---------------- status / last ----------------


def cmd_status(args: argparse.Namespace, config: Config) -> None:
    if args.session:
        data = http_json("GET", f"{config.sidecar}/sessions/{urllib.parse.quote(args.session)}")
    elif args.detail:
        data = http_json("GET", f"{config.sidecar}/sessions")
    else:
        data = http_json("GET", f"{config.sidecar}/status")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_last(args: argparse.Namespace, config: Config) -> None:
    data = http_json(
        "GET",
        f"{config.op_server}/session/{urllib.parse.quote(args.session)}/message?limit={args.limit}",
    )
    if not isinstance(data, list):
        fail("unexpected message response")
    assistant_msgs = [m for m in data if m.get("info", {}).get("role") == "assistant"]
    if not assistant_msgs:
        print("(no assistant messages)")
        return

    def msg_time(msg: dict[str, Any]) -> int | float:
        t = msg.get("info", {}).get("time", {})
        return t.get("completed") or t.get("created") or 0

    last = sorted(assistant_msgs, key=msg_time)[-1]
    texts = [p.get("text", "") for p in last.get("parts", []) if p.get("type") == "text"]
    print("".join(texts) if texts else "(no text parts)")


# ---------------- overview ----------------


def collect_worktree_list(repo: Path) -> list[dict[str, str]]:
    """List all worktrees via ``git worktree list --porcelain``.

    Includes detached worktrees (HEAD not on a local branch) — previously
    silently dropped because the parser only handled ``branch`` lines.
    Detached entries get ``branch=""`` to match ``wt.state`` file convention.
    """
    raw = git(repo, "worktree", "list", "--porcelain", capture=True).stdout
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("worktree "):
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch ") and current is not None:
            branch = line[len("branch ") :].removeprefix("refs/heads/")
            current["branch"] = branch
            current["id"] = Path(current["path"]).name
            entries.append(current)
            current = None
        elif line == "detached" and current is not None:
            # Detached HEAD: append with empty branch (matches wt.state file convention)
            current["branch"] = ""
            current["id"] = Path(current["path"]).name
            entries.append(current)
            current = None
    return entries


def enrich_worktree_status(wt: dict[str, str]) -> None:
    """Mutate ``wt`` with commit / dirty / ahead_main (best-effort)."""
    wt_path = wt["path"]
    try:
        wt["commit"] = git(Path(wt_path), "rev-parse", "--short", "HEAD", capture=True).stdout.strip()
    except subprocess.CalledProcessError:
        wt["commit"] = "?"
    try:
        status = git(Path(wt_path), "status", "--porcelain", capture=True).stdout.strip()
        wt["dirty"] = "dirty" if status else "clean"
    except subprocess.CalledProcessError:
        wt["dirty"] = "?"
    try:
        ahead = git(Path(wt_path), "rev-list", "--count", "HEAD...origin/main", capture=True).stdout.strip()
        wt["ahead_main"] = ahead
    except subprocess.CalledProcessError:
        wt["ahead_main"] = "?"


def collect_wt_sessions(config: Config, wt_id: str, wt_path: str) -> list[dict[str, Any]]:
    """Return sessions for a worktree identified by ``wt_id``/title prefix or directory match.

    Filters:
      - ``wt_N``: ``meta.wt_id == wt_id`` OR ``title.startswith(f"{wt_id}-")``
      - main worktree (``xidi-minimal``): ``directory`` exact match (no ``meta.wt_id``)

    No ``managedBy`` filter — pool-managed, legacy ``worktree_session.py``, and
    user-created sessions (e.g. ``P0-T3-*`` explorations) all surface so overview
    reflects the true session landscape per worktree. Earlier hardcoded
    ``managedBy == "session-worktree-mgr.py"`` filter dropped ~3 sessions per
    wt that were created under the pre-PR-#3 ``worktree_session.py`` name.
    """
    if wt_id == "xidi-minimal":
        # main worktree: sessions with wt_id="main" (sessions create produced)
        # or legacy sessions without any wt_id metadata
        candidates = sessions(config, limit=500)
        out = []
        seen_ids: set[str] = set()
        for s in candidates:
            meta = s.get("metadata", {}) or {}
            sdir = s.get("directory", "")
            sid_wt = meta.get("wt_id")
            if sid_wt and sid_wt != "main":
                continue
            if not sdir or normalize_path(sdir) != normalize_path(wt_path):
                continue
            sid = str(s.get("id") or "")
            if sid:
                seen_ids.add(sid)
            out.append(s)

        # Also materialize PM sessions referenced only by bounded state files.
        # They may be older than the generic /session?limit=500 result window,
        # but the state directory is the source of truth for PM ownership.
        for pm_sid, _state_file, _is_current in recent_pm_state_files(config):
            if not pm_sid or pm_sid in seen_ids:
                continue
            try:
                state_pm_session = http_json(
                    "GET",
                    f"{config.op_server}/session/{urllib.parse.quote(pm_sid)}",
                )
            except SystemExit:
                continue
            if not isinstance(state_pm_session, dict):
                continue
            sdir = state_pm_session.get("directory", "")
            if not sdir or normalize_path(str(sdir)) != normalize_path(wt_path):
                continue
            seen_ids.add(pm_sid)
            out.append(state_pm_session)

    else:
        # wt_N: match by metadata.wt_id or title prefix
        candidates = sessions(config, directory=Path(wt_path), limit=500)
        out = []
        for s in candidates:
            meta = s.get("metadata", {}) or {}
            title = s.get("title", "")
            if meta.get("wt_id") == wt_id or title.startswith(f"{wt_id}-"):
                out.append(s)
    return out


def session_summary(s: dict[str, Any]) -> dict[str, Any]:
    """Reduce a session to id / title / agent / cost / token breakdown for overview output.

    Token breakdown (replaces a single summed ``Tokens`` field — that 20x-distorted
    number hid the 95% cache-hit ratio that's actually driving usage):
      - ``input``:        cumulative input tokens (user prompts)
      - ``out_reason``:   output + reasoning tokens (model-generated, charged at full rate)
      - ``cache_read``:   cumulative cache hits (often the dominant field; cheap rate)
    """
    meta = s.get("metadata", {}) or {}
    tk = s.get("tokens", {}) or {}
    cache = tk.get("cache", {}) or {}
    time_obj = s.get("time") or {}
    return {
        "id": s.get("id", "-"),
        "title": s.get("title", "-"),
        "agent": normalize_agent_label(meta.get("agent") or s.get("agent") or "-"),
        "cost": s.get("cost", 0),
        "input": tk.get("input", 0),
        "out_reason": tk.get("output", 0) + tk.get("reasoning", 0),
        "cache_read": cache.get("read", 0),
        "updated_ms": int(time_obj.get("updated") or 0),
    }


def fmt_tokens(n: int) -> str:
    """Format token count as 12K / 1300 / 800."""
    if n >= 1000:
        return f"{n / 1000:.0f}K"
    return str(n)


def fmt_updated(ms: int) -> str:
    """Format a millisecond timestamp as relative age: now / Xm / Xh / Xd / MM-DD.

    Used in the overview ``Updated`` column to surface stale sessions at a glance
    (cache stays warm ~hours; > 1d old = cache effectively cold for new prompts).
    """
    if not ms:
        return "-"
    delta_s = (int(time.time() * 1000) - ms) / 1000
    if delta_s < 0:
        return "now"
    if delta_s < 60:
        return "now"
    if delta_s < 3600:
        return f"{int(delta_s / 60)}m"
    if delta_s < 86400:
        return f"{int(delta_s / 3600)}h"
    if delta_s < 86400 * 30:
        return f"{int(delta_s / 86400)}d"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%m-%d")


def _parse_duration(value: str) -> int:
    """Parse a duration string into seconds.

    Accepted forms: ``"0"``, ``"<N>d"``, ``"<N>h"``, ``"<N>m"``, ``"<N>s"``.
    Case-insensitive on the unit suffix. Empty string, negative numbers, or
    unknown units call ``fail()`` (which raises SystemExit).
    """
    s = value.strip()
    if not s:
        fail("duration must not be empty")
    if s == "0":
        return 0
    # Must end with a single unit char; rest must be a non-negative integer.
    if len(s) < 2:
        fail(f"invalid duration: {value!r}")
    unit = s[-1].lower()
    body = s[:-1]
    if not body.isdigit():
        fail(f"invalid duration: {value!r}")
    n = int(body)
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    fail(f"invalid duration unit: {value!r} (use s/m/h/d)")


def _apply_recent_filter(
    sessions: list[dict[str, Any]],
    now_ms: int,
    recent_seconds: int,
    pm_session_id: str = "",
) -> list[dict[str, Any]]:
    """Group sessions by ``(wt_id, [pm_session_id,] agent)`` and keep only the recent window.

    Each input dict must carry:

    - ``wt_id``        : str  (worktree id, e.g. ``"wt_1"``); if missing, item is dropped.
    - ``agent``        : str  (e.g. ``"Daedalus"``); if missing, falls back to ``"__unknown__"``.
    - ``updated_ms``   : int  (epoch ms); ``0`` is treated as "unknown age".
    - ``pm_session_id`` (optional): str; only consulted when the ``pm_session_id``
      argument is non-empty (see below).

    Per-group semantics (group key is ``(wt_id, pm, agent)``):

    - If any item has ``updated_ms > now_ms - recent_seconds * 1000`` (strict ``>``),
      keep ONLY the in-window items in the group (strict semantics: ``--recent
      1d`` means "show what updated in the last 1 day, nothing older").
    - Else keep only the single item with the largest ``updated_ms`` (the
      "last-known tombstone") to preserve user-visible history.
    - Empty group → empty output.

    ``pm_session_id`` argument:
      - Empty (default, back-compat): every item's PM bucket is the empty
        string, so the group key collapses to ``(wt_id, agent)`` — identical
        behavior to the original implementation.
      - Non-empty (e.g. ``"pm_session_id"``): treated as the dict key whose
        value should become the per-PM bucket. Two PM sessions that both own
        ``Janitor`` sessions no longer share a single bucket and don't steal
        each other's "last-known tombstone".

    The original order is preserved for retained items (stable within the group);
    groups themselves are emitted in first-seen order.

    Pass ``recent_seconds = 0`` to disable the window: every group collapses
    to its single newest item.
    """
    if recent_seconds < 0:
        fail("recent_seconds must be >= 0")
    threshold_ms = now_ms - recent_seconds * 1000

    # 1. Bucket by (wt_id, pm, agent), preserving first-seen order.
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, str]] = []
    for item in sessions:
        wt_id = item.get("wt_id")
        if not wt_id or not isinstance(wt_id, str):
            # Defensive: callers should always pass wt_id. Drop silently
            # rather than fabricating a bucket key.
            continue
        agent_raw = item.get("agent")
        agent = normalize_agent_label(agent_raw)
        pm = ""
        if pm_session_id:
            pm_raw = item.get(pm_session_id, "")
            pm = pm_raw if isinstance(pm_raw, str) and pm_raw else ""
        key = (wt_id, pm, agent)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)

    # 2. For each bucket, decide keep-in-window-only vs keep-newest.
    out: list[dict[str, Any]] = []
    for key in order:
        items = buckets[key]
        has_recent = any(int(i.get("updated_ms") or 0) > threshold_ms for i in items)
        if has_recent:
            # Strict semantics: --recent N means "only the last N". Older items
            # in the same group are dropped, not "kept for thread continuity"
            # (that interpretation was a spec deviation; see devlog for
            # ``feat_overview_recent_filter_strict``).
            out.extend(i for i in items if int(i.get("updated_ms") or 0) > threshold_ms)
        else:
            # last-known tombstone: max updated_ms; tie-break by first seen.
            newest = max(
                items,
                key=lambda i: (int(i.get("updated_ms") or 0), -items.index(i)),
            )
            out.append(newest)
    return out


def _limit_per_agent(
    sessions: list[dict[str, Any]],
    limits: dict[str, int],
    pm_session_id: str = "",
    default_limit: int = 1,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Keep at most ``limits[agent]`` sessions per agent, newest first by ``updated_ms``.

    When ``pm_session_id`` is set, the limit is applied independently inside
    each PM bucket (read from each item via ``item.get(pm_session_id)``), so
    two PM sessions owning sessions of the same agent do not steal each
    other's slots. When ``pm_session_id`` is empty (default), PM-bucketing is
    skipped — every agent's limit is global (back-compat with the original
    behavior).

    Agents not listed in ``limits`` are NOT dropped silently: they fall back
    to ``default_limit`` (default 1) and a warning is emitted to stderr. This
    preserves the "last-known tombstone" for any new agent the pool learns
    about (e.g. a fresh ``Clio2`` next to the whitelisted ``Clio``) instead
    of vanishing from the overview. Pass ``default_limit=0`` to restore the
    original "drop unlisted agents" behavior.

    ``verbose`` (default False) gates the stderr warning. The fallback
    behavior itself (keep ``default_limit`` sessions for unlisted agents) is
    unchanged — only the warning is silenced unless the caller opts in via
    ``--verbose`` (overview subcommand).
    """
    out: list[dict[str, Any]] = []

    def _select(items: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
        if pm_session_id:
            buckets: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for s in items:
                pm_raw = s.get(pm_session_id, "")
                pm = pm_raw if isinstance(pm_raw, str) and pm_raw else ""
                if pm not in buckets:
                    buckets[pm] = []
                    order.append(pm)
                buckets[pm].append(s)
            selected: list[dict[str, Any]] = []
            for pm in order:
                bucket = buckets[pm]
                bucket.sort(key=lambda x: int(x.get("updated_ms", 0)), reverse=True)
                selected.extend(bucket[:cap])
            return selected
        items.sort(key=lambda x: int(x.get("updated_ms", 0)), reverse=True)
        return items[:cap]

    # 1. Listed agents get their declared limit.
    listed_agents = set(limits.keys())
    for agent, limit in limits.items():
        items = [s for s in sessions if s.get("agent") == agent]
        out.extend(_select(items, limit))

    # 2. Unlisted agents get ``default_limit`` (default 1) + a warning
    # (gated by ``verbose``). Sorted by agent name for stable order across runs.
    unlisted = sorted({s.get("agent") for s in sessions} - listed_agents)  # type: ignore
    for agent in unlisted:
        items = [s for s in sessions if s.get("agent") == agent]
        if verbose:
            eprint(f"warning: agent {agent!r} not in limits; keeping {default_limit} (default_limit)")
        out.extend(_select(items, default_limit))
    return out


def collect_overview(
    config: Config,
    *,
    recent_seconds: int | None = _OVERVIEW_RECENT_DEFAULT_SECONDS,
    show_all: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Build overview payload. Read-mostly: the only side effect is a
    ``watch_session`` call for the current PM's main agents (so the State
    column shows real idle/busy instead of "unknown"). All other
    session-watch restoration is owned by ``sidecar-service`` start/restart
    via ``rewatch_all_sessions``.

    ``recent_seconds`` filters sessions per ``(wt_id, pm_session_id, agent)``
    group (P0-1: per-PM isolation is part of the group key):
      - ``None`` or ``<0`` → no filter applied (legacy show-all behavior).
      - ``0`` → each group collapses to its newest item only.
      - ``>0`` → keep group if any item is within the window, else keep the
        single newest item as a "last-known tombstone".

    ``show_all=True`` overrides ``recent_seconds`` AND the per-agent session
    count limit (P1-1) — parity with the ``--all`` CLI flag.

    ``verbose`` (default False) is forwarded to ``_limit_per_agent`` to gate
    the unlisted-agent fallback warning. Wired from ``--verbose`` (overview
    subcommand).
    """
    wts = collect_worktree_list(config.repo)
    rows: list[dict[str, Any]] = []
    for wt in wts:
        enrich_worktree_status(wt)
        sess_list = collect_wt_sessions(config, wt["id"], wt["path"])
        # Normalize to (wt_id, agent, updated_ms) tuples for the filter, but
        # also keep the raw session dict so we can render after filtering.
        indexed: list[dict[str, Any]] = []
        for s in sess_list:
            meta = s.get("metadata") or {}
            wt_id_meta = meta.get("wt_id")
            wt_id = wt_id_meta if isinstance(wt_id_meta, str) and wt_id_meta else wt["id"]
            agent_raw = meta.get("agent") or s.get("agent")
            updated_ms = int((s.get("time") or {}).get("updated") or 0)
            indexed.append(
                {
                    "_raw": s,
                    "wt_id": wt_id,
                    "agent": normalize_agent_label(agent_raw),
                    "updated_ms": updated_ms,
                    "pm_session_id": "",  # main worktree fills below; others stay ""
                    "pm_current": False,
                }
            )
        # Main worktree: tag every item with its owning PM session BEFORE the
        # recent-window filter and per-agent limit run, so per-PM isolation is
        # honored at the (wt_id, pm_session_id, agent) grouping level. Filter
        # and limit then see the same PM buckets the renderer uses for display.
        if wt["id"] == "xidi-minimal":
            tag_pm_session_ownership(config, indexed)
        if show_all or recent_seconds is None or recent_seconds < 0:
            kept_indexed = indexed
        else:
            kept_indexed = _apply_recent_filter(
                indexed,
                now_ms=int(time.time() * 1000),
                recent_seconds=recent_seconds,
                pm_session_id="pm_session_id",  # per-PM 隔离贯穿 filter
            )
        # Main worktree: per-agent session count limit, scoped per-PM so two
        # PM sessions owning sessions of the same agent do not steal each
        # other's slots. --all overrides this so the user can see every
        # session regardless of count (parity with --all bypassing the
        # recent-window filter above).
        if wt["id"] == "xidi-minimal" and not show_all:
            kept_indexed = _limit_per_agent(
                kept_indexed,
                limits={"PM": 2, "General": 2, "Janitor": 2, "Momus": 2, "Clio": 2},
                pm_session_id="pm_session_id",  # per-PM 隔离贯穿 limit
                verbose=verbose,  # --verbose 控 unlisted-agent warning
            )
            # Cap PM groups to current + the newest historical PM states.
            pm_items = [it for it in kept_indexed if _is_pm_agent(it.get("agent"))]
            pm_groups: dict[str, list[dict[str, Any]]] = {}
            for it in pm_items:
                gid = it.get("pm_session_id", "")
                pm_groups.setdefault(gid, []).append(it)
            pm_group_limit = _PM_STATE_HISTORY_LIMIT_DEFAULT + (1 if any(any(i.get("pm_current") for i in group) for group in pm_groups.values()) else 0)
            if len(pm_groups) > pm_group_limit:
                sorted_groups = sorted(
                    pm_groups.items(),
                    key=lambda kv: (
                        0 if any(i.get("pm_current") for i in kv[1]) else 1,
                        -max(int(i.get("updated_ms", 0)) for i in kv[1]),
                    ),
                )
                drop_sids = {sid for sid, _ in sorted_groups[pm_group_limit:]}
                kept_indexed = [it for it in kept_indexed if it.get("pm_session_id", "") not in drop_sids]
            # Ensure all PM-owned main agents are watched by sidecar so State
            # column shows real idle/busy instead of "unwatch"
            for it in kept_indexed:
                if it.get("pm_session_id"):
                    try:
                        watch_session(config, it["_raw"].get("id", ""))
                    except SystemExit:
                        pass
        rows.append(
            {
                **wt,
                "sessions": [
                    {
                        **session_summary(it["_raw"]),
                        "context": fetch_session_context(config, it["_raw"].get("id", "-")),
                        "pm_session_id": it.get("pm_session_id", ""),
                        "pm_current": it.get("pm_current", False),
                    }
                    for it in kept_indexed
                ],
            }
        )
    # Fetch sidecar /status once for the State column; unwatched sessions
    # are reported as "unknown" (sidecar only tracks registered sessions).
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    if not isinstance(status_map, dict):
        status_map = {}
    return {
        "time": now_utc(),
        "health": {
            "opencode": op_healthy(config),
            "sidecar": sidecar_healthy(config),
        },
        "worktrees": rows,
        "sidecar_status_map": status_map,
    }


def fetch_last_reply(config: Config, session_id: str, limit: int = 50) -> str | None:
    """Fetch last assistant message text for a session; ``None`` on failure."""
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit={limit}",
        )
    except SystemExit:
        return None
    if not isinstance(data, list):
        return None
    msgs = [m for m in data if m.get("info", {}).get("role") == "assistant"]
    if not msgs:
        return None

    def msg_time(m: dict[str, Any]) -> int | float:
        t = m.get("info", {}).get("time", {})
        return t.get("completed") or t.get("created") or 0

    last = sorted(msgs, key=msg_time)[-1]
    texts = [p.get("text", "") for p in last.get("parts", []) if p.get("type") == "text"]
    return "".join(texts) if texts else None


def fetch_session_context(config: Config, session_id: str) -> int:
    """Return the context window tokens used in the session's latest LLM call.

    Reads the last message (``GET /session/{id}/message?limit=1``) and extracts
    ``input + cache.read`` from the ``step-finish`` part — this is the actual
    token count that was sent to the model in the most recent call (cached
    prefix hits included). Distinct from ``session.tokens`` which is cumulative
    across the whole session lifetime.

    Returns ``0`` on fetch failure, no step-finish, or empty ``session_id``.
    """
    if not session_id or session_id == "-":
        return 0
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit=1",
        )
    except SystemExit:
        return 0
    if not isinstance(data, list) or not data:
        return 0
    for msg in data:
        for part in msg.get("parts") or []:
            if part.get("type") == "step-finish":
                tk = part.get("tokens", {}) or {}
                cache = tk.get("cache", {}) or {}
                return int(tk.get("input", 0)) + int(cache.get("read", 0))
    return 0


def _print_session_rows(
    wt: dict[str, Any],
    sessions: list[dict[str, Any]],
    status_map: dict[str, str],
    detail: bool,
    config: Config,
    *,
    show_unwatch: bool = False,
) -> None:
    """Print one worktree's session rows.

    ``wt['id'] == 'xidi-minimal'`` suppresses the WT column (PM session label
    already printed by the caller for grouped main-worktree sessions).

    ``show_unwatch`` (default False) hides sessions whose sidecar state is
    ``unwatch``. Pass ``--show-unwatch`` to include them.
    """
    is_main = wt["id"] == "xidi-minimal"
    if not show_unwatch:
        sessions = [s for s in sessions if str(status_map.get(s.get("id", "-"), "unwatch")) not in ("unwatch", "unknown")]
    if not sessions:
        return
    first = True
    for sess in sessions:
        sid = sess.get("id", "-")
        sagent = sess.get("agent", "-")
        sin = fmt_tokens(sess.get("input", 0))
        sout = fmt_tokens(sess.get("out_reason", 0))
        scr = fmt_tokens(sess.get("cache_read", 0))
        inp = int(sess.get("input", 0))
        cr = int(sess.get("cache_read", 0))
        total_in = inp + cr
        hit_pct = f"{cr * 100 // total_in}%" if total_in > 0 else "—"
        sctx = fmt_tokens(sess.get("context", 0))
        supd = fmt_updated(sess.get("updated_ms", 0))
        sstate = str(status_map.get(sid, "unwatch"))
        if sstate == "unknown":
            sstate = "unwatch"
        if is_main:
            prefix = f"  {'':<14} {'':<40} {'':<9} {'':<7} {'':<6}"
        else:
            prefix = f"  {wt['id']:<14} {wt['branch']:<40} {wt['commit']:<9} {wt['dirty']:<7} {wt['ahead_main']:<6}" if first else f"  {'':<14} {'':<40} {'':<9} {'':<7} {'':<6}"
        first = False
        row = f"{prefix} {sagent:<10} {sin:<7} {sout:<8} {scr:<8} {hit_pct:<6} {sctx:<8} {supd:<7} {sstate:<8} {sid}"
        print(row)
        if detail and sid != "-":
            reply = fetch_last_reply(config, sid)
            if reply:
                print("    ── 最后回复 ──")
                for line in reply.splitlines():
                    print(f"    {line}")
                print()


def print_overview_text(payload: dict[str, Any], detail: bool, config: Config, *, show_orphan: bool = False, show_unwatch: bool = False) -> None:
    """Render overview payload as a fixed-width text table.

    ``show_orphan`` (default False) controls whether main-worktree sessions
    that do not belong to any PM session (no matching ``main.state`` entry,
    no current-PM self-group) are surfaced. Hidden by default to keep
    overview compact; pass ``--show-orphan`` to include the orphan group.
    """
    oh = payload["health"]["opencode"]
    sh = payload["health"]["sidecar"]
    print()
    print(f"══ Session Status — {payload['time']} ══")
    print()
    print("── 服务健康 ──")
    print(f"  OpenCode : {'healthy' if oh else 'down'} ({config.op_server})")
    print(f"  Sidecar  : {'healthy' if sh else 'down'}")
    print()
    print("── Worktree ──")
    status_map = payload.get("sidecar_status_map", {})
    header = (
        f"  {'WT':<14} {'Branch':<40} {'Commit':<9} {'Dirty':<7} {'Δmain':<6} "
        f"{'Agent':<10} {'Input':<7} {'Out+Rea':<8} {'Cache.R':<8} {'Hit%':<6} "
        f"{'Context':<8} {'Updated':<7} {'State':<8} {'Session ID'}"
    )
    sep = f"  {'-' * 14:<14} {'-' * 40:<40} {'-' * 9:<9} {'-' * 7:<7} {'-' * 6:<6} {'-' * 10:<10} {'-' * 7:<7} {'-' * 8:<8} {'-' * 8:<8} {'-' * 6:<6} {'-' * 8:<8} {'-' * 7:<7} {'-' * 8:<8} {'-' * 30}"
    print(header)
    print(sep)
    for wt in payload["worktrees"]:
        sessions = wt.get("sessions", [])
        if not sessions:
            print(f"  {wt['id']:<14} {wt['branch']:<40} {wt['commit']:<9} {wt['dirty']:<7} {wt['ahead_main']:<6} (无)")
            continue
        is_main = wt["id"] == "xidi-minimal"
        # Group main-worktree sessions by PM session for clean grouping
        if is_main:
            groups: dict[str, list[dict[str, Any]]] = {}
            group_order: list[str] = []
            for sess in sessions:
                pm_sid = sess.get("pm_session_id", "") or "orphan"
                if pm_sid not in groups:
                    groups[pm_sid] = []
                    group_order.append(pm_sid)
                groups[pm_sid].append(sess)
            # Sort each group: PM first, then agents alphabetically
            for g in group_order:
                groups[g].sort(key=lambda s: (0 if _is_pm_agent(s.get("agent")) else 1, s.get("agent", "")))
            # Print current PM session first, then others
            current_first: list[str] = []
            others: list[str] = []
            for g in group_order:
                if any(sess.get("pm_current") for sess in groups[g]):
                    current_first.append(g)
                else:
                    others.append(g)
            group_order = current_first + others
            first_wt = True
            for grp in group_order:
                if not show_orphan and grp == "orphan":
                    continue
                grp_sessions = groups[grp]
                # Group header
                if grp == "orphan":
                    label = "── 未归属（orphan）──"
                elif any(sess.get("pm_current") for sess in grp_sessions):
                    label = f"── PM {grp}（当前）──"
                else:
                    label = f"── PM {grp} ──"
                if not first_wt:
                    print()
                print(f"  {label}")
                first_wt = False
                _print_session_rows(wt, grp_sessions, status_map, detail, config, show_unwatch=show_unwatch)
        else:
            _print_session_rows(wt, sessions, status_map, detail, config, show_unwatch=show_unwatch)
    print()


def cmd_overview(args: argparse.Namespace, config: Config) -> None:
    """Show project-wide / single-wt / single-session overview.

    Replaces the legacy worktree_session_status.py.
    """
    if args.session and (args.wt or getattr(args, "main", False)):
        fail("--session is mutually exclusive with --wt/--main")
    if args.wt and getattr(args, "main", False):
        fail("--wt and --main are mutually exclusive")
    if args.session:
        cmd_overview_session(args, config)
        return
    if getattr(args, "main", False):
        args.wt = config.repo.name
    if args.wt:
        # Accept --wt 10 as shorthand for --wt wt_10
        if args.wt.isdigit():
            args.wt = f"wt_{args.wt}"
        cmd_overview_wt(args, config)
        return
    show_all = bool(getattr(args, "all", False))
    if show_all:
        recent_seconds: int | None = None
        args.show_unwatch = True  # --all implies --show-unwatch
        args.show_unwatch = True  # --all implies --show-unwatch
    else:
        recent_arg = getattr(args, "recent", None)
        if recent_arg is None:
            recent_seconds = _OVERVIEW_RECENT_DEFAULT_SECONDS
        else:
            recent_seconds = _parse_duration(recent_arg)
    payload = collect_overview(
        config,
        recent_seconds=recent_seconds,
        show_all=show_all,
        verbose=bool(getattr(args, "verbose", False)),
    )
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    show_orphan = bool(getattr(args, "show_orphan", False))
    show_unwatch = bool(getattr(args, "show_unwatch", False))
    print_overview_text(payload, args.detail, config, show_orphan=show_orphan, show_unwatch=show_unwatch)


def cmd_overview_session(args: argparse.Namespace, config: Config) -> None:
    """Show a single session's full state — any session, not limited to worktree.

    Useful for: main worktree's PM session, orphaned sessions, subagent sessions.
    """
    ses_id = args.session
    data = http_json(
        "GET",
        f"{config.op_server}/session/{urllib.parse.quote(ses_id)}",
    )
    if not isinstance(data, dict):
        fail(f"unexpected /session response: {type(data).__name__}")
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    print(f"══ Session {ses_id} ══")
    print()
    print(f"  Title      : {data.get('title', '-')}")
    print(f"  Agent      : {data.get('agent', '-')}")
    model = data.get("model") or {}
    print(f"  Model      : {model.get('id', '-')} ({model.get('providerID', '-')}{':' + model.get('variant', '-') if model.get('variant') else ''})")
    print(f"  Directory  : {data.get('directory', '-')}")
    parent = data.get("parentID")
    print(f"  Parent     : {parent or '-'}")

    # Sidecar state (idle / busy / unknown)
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
        if isinstance(status_map, dict):
            state = str(status_map.get(ses_id, "unknown"))
        else:
            state = "unknown"
    except SystemExit:
        state = "unknown"
    print(f"  State      : {state}")
    time_obj = data.get("time") or {}
    if time_obj.get("created"):
        from datetime import UTC, datetime

        created = datetime.fromtimestamp(time_obj["created"] / 1000, tz=UTC).isoformat()
        print(f"  Created    : {created}")
    if time_obj.get("updated"):
        from datetime import UTC, datetime

        updated = datetime.fromtimestamp(time_obj["updated"] / 1000, tz=UTC).isoformat()
        print(f"  Updated    : {updated}")

    tk = data.get("tokens") or {}
    cache = tk.get("cache") or {}
    inp = tk.get("input", 0)
    cread = cache.get("read", 0)
    cwrite = cache.get("write", 0)
    print()
    print("  Tokens (cumulative):")
    print(f"    Input     : {fmt_tokens(inp)}")
    print(f"    Out+Rea   : {fmt_tokens(tk.get('output', 0) + tk.get('reasoning', 0))}")
    print(f"    Cache.R   : {fmt_tokens(cread)}")
    print(f"    Cache.W   : {fmt_tokens(cwrite)}")
    cost = data.get("cost")
    if cost is not None:
        print(f"    Cost      : ${cost:.4f}")

    # Cache hit rate: cumulative cache.read / (input + cache.read)
    # = fraction of total LLM-bound input that came from cache (i.e. didn't need recompute)
    total_in = inp + cread
    if total_in > 0:
        hit_pct = (cread / total_in) * 100
        print()
        print("  Cache efficiency:")
        print(f"    Hit rate  : {hit_pct:5.1f}%  ({fmt_tokens(cread)} cached / {fmt_tokens(total_in)} total input)")

    print()
    ctx = fetch_session_context(config, ses_id)
    print(f"  Context (current LLM window): {fmt_tokens(ctx)}")

    meta = data.get("metadata") or {}
    if meta:
        print()
        print("  Metadata:")
        for k, v in sorted(meta.items()):
            print(f"    {k:<11}: {v}")

    if args.detail:
        reply = fetch_last_reply(config, ses_id)
        if reply:
            print()
            print("  ── Last reply ──")
            for line in reply.splitlines():
                print(f"  {line}")


def cmd_overview_wt(args: argparse.Namespace, config: Config) -> None:
    """Show one worktree's session pool only.

    For the main worktree, parity with ``collect_overview`` is enforced:
    recent-window filter, per-agent session count limit, and PM-session
    grouping all run on the same indexed pipeline so the rendered table
    matches what ``overview`` would show for that single worktree. For
    pool worktrees (wt_N) only the recent-window filter applies — no PM
    grouping, no per-agent limit (those are main-worktree concepts only).
    """
    wt_id = args.wt
    wts = collect_worktree_list(config.repo)
    target = next((w for w in wts if w["id"] == wt_id), None)
    if target is None:
        known = ", ".join(w["id"] for w in wts) or "(none)"
        fail(f"worktree not found: {wt_id} (known: {known})")
    enrich_worktree_status(target)
    sess_list = collect_wt_sessions(config, target["id"], target["path"])

    # Resolve --all / --recent the same way cmd_overview does, so callers
    # see consistent behavior across the two entry points.
    show_all = bool(getattr(args, "all", False))
    if show_all:
        recent_seconds: int | None = None
        args.show_unwatch = True  # --all implies --show-unwatch
    else:
        recent_arg = getattr(args, "recent", None)
        if recent_arg is None:
            recent_seconds = _OVERVIEW_RECENT_DEFAULT_SECONDS
        else:
            recent_seconds = _parse_duration(recent_arg)

    # Normalize to (wt_id, agent, updated_ms) tuples for the filter, but
    # also keep the raw session dict so we can render after filtering.
    indexed: list[dict[str, Any]] = []
    for s in sess_list:
        meta = s.get("metadata") or {}
        wt_id_meta = meta.get("wt_id")
        item_wt_id = wt_id_meta if isinstance(wt_id_meta, str) and wt_id_meta else target["id"]
        agent_raw = meta.get("agent") or s.get("agent")
        updated_ms = int((s.get("time") or {}).get("updated") or 0)
        indexed.append(
            {
                "_raw": s,
                "wt_id": item_wt_id,
                "agent": normalize_agent_label(agent_raw),
                "updated_ms": updated_ms,
                "pm_session_id": "",
                "pm_current": False,
            }
        )

    # Main worktree: tag every item with its owning PM session BEFORE the
    # recent-window filter and per-agent limit run, so per-PM isolation is
    # honored at the (wt_id, pm_session_id, agent) grouping level.
    if target["id"] == "xidi-minimal":
        tag_pm_session_ownership(config, indexed)

    if show_all or recent_seconds is None or recent_seconds < 0:
        kept_indexed = indexed
    else:
        kept_indexed = _apply_recent_filter(
            indexed,
            now_ms=int(time.time() * 1000),
            recent_seconds=recent_seconds,
            pm_session_id="pm_session_id",
        )

    # Main worktree: per-agent session count limit, scoped per-PM.
    # --all overrides this so the user sees every session regardless of count.
    if target["id"] == "xidi-minimal" and not show_all:
        kept_indexed = _limit_per_agent(
            kept_indexed,
            limits={"PM": 2, "General": 2, "Janitor": 2, "Momus": 2, "Clio": 2},
            pm_session_id="pm_session_id",
            verbose=bool(getattr(args, "verbose", False)),
        )
        # Cap PM groups to current + the newest historical PM states.
        pm_items = [it for it in kept_indexed if _is_pm_agent(it.get("agent"))]
        pm_groups: dict[str, list[dict[str, Any]]] = {}
        for it in pm_items:
            gid = it.get("pm_session_id", "")
            pm_groups.setdefault(gid, []).append(it)
        pm_group_limit = _PM_STATE_HISTORY_LIMIT_DEFAULT + (1 if any(any(i.get("pm_current") for i in group) for group in pm_groups.values()) else 0)
        if len(pm_groups) > pm_group_limit:
            sorted_groups = sorted(
                pm_groups.items(),
                key=lambda kv: (
                    0 if any(i.get("pm_current") for i in kv[1]) else 1,
                    -max(int(i.get("updated_ms", 0)) for i in kv[1]),
                ),
            )
            drop_sids = {sid for sid, _ in sorted_groups[pm_group_limit:]}
            kept_indexed = [it for it in kept_indexed if it.get("pm_session_id", "") not in drop_sids]
        # Ensure all PM-owned main agents are watched by sidecar so State
        # column shows real idle/busy instead of "unwatch"
        for it in kept_indexed:
            if it.get("pm_session_id"):
                try:
                    watch_session(config, it["_raw"].get("id", ""))
                except SystemExit:
                    pass

    row = {
        **target,
        "sessions": [
            {
                **session_summary(it["_raw"]),
                "context": fetch_session_context(config, it["_raw"].get("id", "-")),
                "pm_session_id": it.get("pm_session_id", ""),
                "pm_current": it.get("pm_current", False),
            }
            for it in kept_indexed
        ],
    }
    if args.format == "json":
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return
    # Fetch sidecar /status once for the State column (parity with cmd_overview).
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    if not isinstance(status_map, dict):
        status_map = {}
    payload = {
        "time": now_utc(),
        "health": {
            "opencode": op_healthy(config),
            "sidecar": sidecar_healthy(config),
        },
        "worktrees": [row],
        "sidecar_status_map": status_map,
    }
    show_orphan = bool(getattr(args, "show_orphan", False))
    show_unwatch = bool(getattr(args, "show_unwatch", False))
    print_overview_text(payload, args.detail, config, show_orphan=show_orphan, show_unwatch=show_unwatch)


# ---------------- idle-watch ----------------


def _idle_safe_name(value: str) -> str:
    """Sanitize session ID for use in pid/log filenames.
    Keep alnum + -_. ; replace others with _.
    """
    chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in "-_.":
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars)


def _idle_pidfile(config: Config, session: str) -> Path:
    return pid_file(config, f"watch-session-idle-{_idle_safe_name(session)}")


def _idle_logfile(config: Config, session: str) -> Path:
    return log_file(config, f"watch-session-idle-{_idle_safe_name(session)}")


def _idle_validate_ses(name: str, value: str) -> str:
    """Validate session ID is non-empty and starts with 'ses'."""
    if not value:
        fail(f"{name} must be non-empty")
    if not value.startswith("ses"):
        fail(f"{name} must start with 'ses': {value}")
    return value


def _idle_fetch_status(config: Config, session: str) -> str:
    """GET sidecar /status and return state for `session`.

    Best-effort: if target is missing from the map, register via
    watch_session() and re-fetch. Returns 'unknown' on any HTTP error.
    """
    try:
        payload = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    state = payload.get(session)
    if state is None:
        try:
            watch_session(config, session)
            payload = http_json("GET", f"{config.sidecar}/status")
        except SystemExit:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        state = payload.get(session)
    return state if isinstance(state, str) else "unknown"


def _idle_prompt_async(
    config: Config,
    notify_session: str,
    message: str,
    *,
    directory: str | None = None,
    workspace: str | None = None,
    timeout: float | None = None,
) -> bool:
    """POST prompt_async to op-server for notify_session. Return True on HTTP 204."""
    query_items: list[str] = []
    if directory:
        query_items.append("directory=" + urllib.parse.quote(directory, safe=""))
    if workspace:
        query_items.append("workspace=" + urllib.parse.quote(workspace, safe=""))
    query = "?" + "&".join(query_items) if query_items else ""
    url = f"{config.op_server}/session/{notify_session}/prompt_async{query}"
    body = {"parts": [{"type": "text", "text": message}]}
    effective_timeout = int(timeout) if timeout is not None else config.http_timeout
    try:
        http_json(
            "POST",
            url,
            body,
            expected=(204,),
            timeout=effective_timeout,
        )
        return True
    except SystemExit:
        return False


def _session_has_assistant_reply_after(config: Config, session_id: str, started_at_ms: int, limit: int = 50) -> bool:
    """Return True when the session has an assistant reply after ``started_at_ms``.

    This is used only by dispatch-spawned idle-watch as a race-condition
    fallback: if a task finishes before the watcher ever observes
    busy/streaming, the watcher can still notify exactly once after it sees
    that the target session produced a new assistant message.
    """
    if started_at_ms <= 0:
        return False
    try:
        data = http_json(
            "GET",
            f"{config.op_server}/session/{urllib.parse.quote(session_id)}/message?limit={limit}",
        )
    except SystemExit:
        return False
    if not isinstance(data, list):
        return False
    for msg in data:
        if msg.get("info", {}).get("role") != "assistant":
            continue
        t = msg.get("info", {}).get("time", {}) or {}
        msg_ms = int(t.get("completed") or t.get("created") or 0)
        if msg_ms >= started_at_ms:
            return True
    return False


def _build_idle_notify_message(
    config: Config,
    target: str,
    notify_reason: str,
    custom_message: str | None,
) -> str:
    """Compose the prompt_async body sent to the notify session.

    Two modes:

    - ``custom_message`` is set (user passed ``--message``): that exact text is
      sent verbatim. The caller has full control; no tag is added (the watcher
      shouldn't second-guess an explicit user override).
    - Default mode: prefix ``[idle-notify]`` so the receiving session can grep
      auto-notifies, then include the target session's last assistant reply at
      the moment of the busy→idle transition. The tag suffix encodes *why* the
      notify fired (busy→idle edge vs. initial-idle tick) so the receiver can
      tell them apart without cross-referencing logs.

      The last-reply fetch is best-effort: a session that just transitioned to
      idle but hasn't produced a final assistant message (e.g. failed tasks)
      yields a stub that still carries the tag + target id, so the receiver
      knows the watcher fired but content is unavailable.
    """
    if custom_message is not None:
        return custom_message

    if "busy -> idle" in notify_reason:
        tag = "[idle-notify:busy->idle]"
    elif "idle after dispatch update" in notify_reason:
        tag = "[idle-notify:idle-after-update]"
    elif "initial" in notify_reason:
        tag = "[idle-notify:initial-idle]"
    else:
        tag = "[idle-notify]"

    last_reply = fetch_last_reply(config, target)
    if last_reply is None:
        return f"{tag} target={target} (no assistant message found)"
    return f"{tag} target={target}\n\nLast assistant message:\n\n{last_reply}\n"


_IDLE_WATCH_INTERVAL_DEFAULT = 2.0
_IDLE_WATCH_TIMEOUT_DEFAULT = 10.0
_IDLE_WATCH_MAX_ERRORS_DEFAULT = 10
_IDLE_WATCH_STOP_TIMEOUT_DEFAULT = 3.0


def _spawn_dispatch_idle_watch(
    config: Config,
    target_sid: str,
    notify_sid: str,
    wt_path: Path,
    *,
    max_poll_seconds: int = 0,
    started_at_ms: int = 0,
) -> None:
    """Spawn a one-shot idle-watch for the dispatched session.

    Monitors sidecar /status for busy→idle transition on ``target_sid``, then
    sends a prompt_async to ``notify_sid`` (typically the PM session) and exits.

    The watcher runs as a detached background process; its pid/log are tracked
    alongside other idle-watch instances via ``_idle_pidfile`` / ``_idle_logfile``.

    ``max_poll_seconds`` (default 0 = unlimited) is forwarded to ``idle-watch`` as
    ``--max-poll-seconds``. Pass a positive value for tasks that may take longer
    than the previous hardcoded 600s budget (e.g. Daedalus 30min → 1800).

    ``started_at_ms`` enables the dispatch-aware idle-after-update fallback for
    very short tasks that complete before the watcher observes busy/streaming.
    """
    pf = _idle_pidfile(config, target_sid)
    lf = _idle_logfile(config, target_sid)

    old_pid = read_pid(pf)
    if pid_alive(old_pid):
        eprint(f"[dispatch] idle-watch already running for {target_sid} (pid {old_pid}); skip spawn")
        return

    if pf.exists():
        pf.unlink(missing_ok=True)
    lf.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "idle-watch",
        "--session",
        target_sid,
        "--notify-session",
        notify_sid,
        "--directory",
        str(wt_path),
    ]
    if max_poll_seconds > 0:
        cmd.extend(["--max-poll-seconds", str(int(max_poll_seconds))])
    if started_at_ms > 0:
        cmd.extend(["--started-at-ms", str(int(started_at_ms))])
        cmd.append("--notify-if-idle-after-update")
    log_fd = open(lf, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(config.repo),
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_fd.close()

    pf.write_text(str(proc.pid), encoding="utf-8")
    eprint(f"[dispatch] auto idle-watch spawned: target={target_sid} notify={notify_sid} pid={proc.pid}")


def cmd_idle_watch(args: argparse.Namespace, config: Config) -> None:
    """Foreground: poll sidecar /status; send prompt_async on busy->idle edge.

    Without --continuous, exit after first notify. With --continuous, loop
    forever. --notify-if-initial-idle also sends a prompt if first tick is
    already 'idle'.
    """
    target = _idle_validate_ses("--session", args.session)
    notify = _idle_validate_ses("--notify-session", args.notify_session)

    interval = args.interval
    timeout = args.timeout
    max_errors = args.max_errors
    continuous = bool(getattr(args, "continuous", False))
    initial_idle_notify = bool(getattr(args, "notify_if_initial_idle", False))
    idle_after_update_notify = bool(getattr(args, "notify_if_idle_after_update", False))
    started_at_ms = int(getattr(args, "started_at_ms", 0) or 0)

    if interval <= 0:
        fail("--interval must be > 0")
    if timeout <= 0:
        fail("--timeout must be > 0")
    if max_errors <= 0:
        fail("--max-errors must be > 0")
    if started_at_ms < 0:
        fail("--started-at-ms must be >= 0")
    if idle_after_update_notify and started_at_ms <= 0:
        fail("--notify-if-idle-after-update requires --started-at-ms")

    max_poll_seconds = getattr(args, "max_poll_seconds", 0) or 0
    deadline: float | None = None
    if max_poll_seconds > 0:
        deadline = time.monotonic() + max_poll_seconds

    # If the user passed --message explicitly, that wins. Otherwise the watcher
    # auto-builds a tagged notify containing the target session's last assistant
    # reply at the moment of the transition (much more useful for the receiving
    # session than the old generic "check session X last message" placeholder).
    custom_message = args.message if args.message else None

    eprint(f"[idle-watch] target={target} notify={notify} interval={interval} custom_message={custom_message!r}")

    previous_status: str | None = None
    consecutive_errors = 0
    saw_busy_or_streaming = False
    idle_after_update_notified = False

    while True:
        if deadline is not None and time.monotonic() > deadline:
            eprint(f"[idle-watch] max poll seconds ({max_poll_seconds}s) reached; exiting")
            return
        current_status = _idle_fetch_status(config, target)

        if current_status == "unknown" and previous_status is None and not initial_idle_notify:
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                fail(f"sidecar /status returned 'unknown' for {target} after {max_errors} consecutive errors; pass --notify-if-initial-idle to send on first tick")
            time.sleep(interval)
            continue
        # P1 fix: only reset the error counter on a known status. Letting the
        # reset fire for post-baseline 'unknown' would clobber consecutive_errors
        # every tick and prevent the post-baseline handler below from
        # accumulating toward max_errors.
        if current_status != "unknown":
            consecutive_errors = 0
        # P1 fix: post-baseline 'unknown' must NOT clobber previous_status
        # (e.g. "busy"); otherwise a busy->idle transition that happens between
        # the transient sidecar error and the next successful poll is masked
        # (transition would be observed as unknown->idle, not busy->idle).
        if current_status == "unknown" and previous_status is not None:
            consecutive_errors += 1
            eprint(f"[idle-watch] sidecar /status returned 'unknown' for {target} [{consecutive_errors}/{max_errors}]; keeping previous_status={previous_status!r}")
            if consecutive_errors >= max_errors:
                fail(f"sidecar /status returned 'unknown' for {target} after {max_errors} consecutive errors; aborting to avoid masking busy->idle transition")
            time.sleep(interval)
            continue

        if current_status in ("busy", "streaming"):
            saw_busy_or_streaming = True

        should_notify = False
        notify_reason = ""

        if previous_status is None:
            eprint(f"[idle-watch] initial status: {current_status}")
            if initial_idle_notify and current_status == "idle":
                should_notify = True
                notify_reason = "initial idle (notify-if-initial-idle)"
        elif current_status != previous_status:
            eprint(f"[idle-watch] status changed: {previous_status} -> {current_status}")
            if previous_status in ("busy", "streaming") and current_status == "idle":
                should_notify = True
                notify_reason = "busy -> idle"

        if (
            not should_notify
            and idle_after_update_notify
            and not saw_busy_or_streaming
            and not idle_after_update_notified
            and current_status == "idle"
            and _session_has_assistant_reply_after(config, target, started_at_ms)
        ):
            should_notify = True
            notify_reason = "idle after dispatch update"

        if should_notify:
            eprint(f"[idle-watch] detected {notify_reason}, sending prompt_async to {notify}")
            message_to_send = _build_idle_notify_message(config, target, notify_reason, custom_message)
            ok = _idle_prompt_async(
                config,
                notify,
                message_to_send,
                directory=getattr(args, "directory", None),
                workspace=getattr(args, "workspace", None),
                timeout=timeout,
            )
            if not ok:
                eprint("[idle-watch] prompt_async failed; will retry next tick")
            else:
                eprint("[idle-watch] async prompt accepted (204)")
                if notify_reason == "idle after dispatch update":
                    idle_after_update_notified = True
                if not continuous:
                    return

        previous_status = current_status
        time.sleep(interval)


def cmd_idle_watch_start(args: argparse.Namespace, config: Config) -> None:
    """Background: spawn idle-watch as a detached process with pid+log file."""
    target = _idle_validate_ses("--session", args.session)
    notify = _idle_validate_ses("--notify-session", args.notify_session)

    pf = _idle_pidfile(config, target)
    lf = _idle_logfile(config, target)

    old_pid = read_pid(pf)
    if old_pid is not None and pid_alive(old_pid):
        eprint(f"[idle-watch] {target}: watcher already running (pid {old_pid}, {pf})")
        raise SystemExit(1)

    if pf.exists():
        pf.unlink(missing_ok=True)

    lf.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "idle-watch",
        "--session",
        target,
        "--notify-session",
        notify,
        "--interval",
        str(args.interval),
        "--timeout",
        str(args.timeout),
        "--max-errors",
        str(args.max_errors),
    ]
    # Only forward --message when the caller explicitly set it. If omitted, the
    # child's auto-build path (fetch_last_reply + [idle-notify] tag) takes over.
    if args.message:
        cmd.extend(["--message", args.message])
    if getattr(args, "max_poll_seconds", 0):
        cmd.extend(["--max-poll-seconds", str(args.max_poll_seconds)])
    if getattr(args, "directory", None):
        cmd += ["--directory", args.directory]
    if getattr(args, "workspace", None):
        cmd += ["--workspace", args.workspace]
    if getattr(args, "continuous", False):
        cmd.append("--continuous")
    if getattr(args, "started_at_ms", 0):
        cmd.extend(["--started-at-ms", str(int(args.started_at_ms))])
    if getattr(args, "notify_if_initial_idle", False):
        cmd.append("--notify-if-initial-idle")
    if getattr(args, "notify_if_idle_after_update", False):
        cmd.append("--notify-if-idle-after-update")

    log_fd = open(lf, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(config.repo),
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_fd.close()

    pf.write_text(str(proc.pid), encoding="utf-8")

    eprint(f"[idle-watch] started target={target} notify={notify} pid={proc.pid} log={lf}")


def cmd_idle_watch_stop(args: argparse.Namespace, config: Config) -> None:
    """Stop background watcher: SIGTERM, escalate to SIGKILL on --force."""
    target = _idle_validate_ses("--session", args.session)
    pf = _idle_pidfile(config, target)
    pid = read_pid(pf)

    if pid is None:
        eprint(f"[idle-watch] {target}: not running (no pid file at {pf})")
        return

    if not pid_alive(pid):
        eprint(f"[idle-watch] {target}: stale pid file (pid {pid} not alive); removing")
        pf.unlink(missing_ok=True)
        return

    eprint(f"[idle-watch] {target}: sending SIGTERM to pid {pid}")
    term_sent = False
    try:
        os.killpg(pid, signal.SIGTERM)
        term_sent = True
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        term_sent = True
    except (ProcessLookupError, PermissionError):
        if not term_sent:
            raise

    stop_timeout = args.stop_timeout
    deadline = time.monotonic() + stop_timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            eprint(f"[idle-watch] {target}: stopped (pid {pid})")
            pf.unlink(missing_ok=True)
            return
        time.sleep(0.1)

    if args.force:
        eprint(f"[idle-watch] {target}: SIGTERM timeout; sending SIGKILL to pid {pid}")
        kill_sent = False
        try:
            os.killpg(pid, signal.SIGKILL)
            kill_sent = True
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            kill_sent = True
        except (ProcessLookupError, PermissionError):
            if not kill_sent:
                raise
        time.sleep(0.1)
        if pid_alive(pid):
            fail(f"failed to kill pid {pid} even with SIGKILL")
        eprint(f"[idle-watch] {target}: killed (pid {pid})")
        pf.unlink(missing_ok=True)
        return

    fail(f"timed out after {stop_timeout}s waiting for pid {pid} to exit; pass --force to SIGKILL")


def cmd_idle_watch_status(args: argparse.Namespace, config: Config) -> None:
    """Check background watcher state. Exit 0 running, 1 no pid, 2 stale."""
    target = _idle_validate_ses("--session", args.session)
    pf = _idle_pidfile(config, target)
    lf = _idle_logfile(config, target)
    pid = read_pid(pf)

    if pid is None:
        eprint(f"[idle-watch] {target}: not running (no pid file at {pf})")
        raise SystemExit(1)

    if not pid_alive(pid):
        eprint(f"[idle-watch] {target}: stale pid file (pid {pid} not alive)")
        raise SystemExit(2)

    eprint(f"[idle-watch] {target}: running (pid {pid}, log {lf})")


def cmd_watch_status(args: argparse.Namespace, config: Config) -> None:
    """Compatibility wrapper for ``watch status``.

    By default this mirrors the legacy top-level ``status`` command, so:
      - ``watch status`` -> sidecar /status
      - ``watch status --detail`` -> sidecar /sessions
      - ``watch status --session ses_xxx`` -> sidecar /sessions/{id}

    Use ``--watcher-process --session ses_xxx`` for the old detached
    idle-watch PID/log status check.
    """
    if getattr(args, "watcher_process", False):
        if not getattr(args, "session", None):
            fail("watch status --watcher-process requires --session")
        cmd_idle_watch_status(args, config)
        return
    ns = argparse.Namespace(
        session=getattr(args, "session", None),
        detail=bool(getattr(args, "detail", False)),
    )
    cmd_status(ns, config)


def cmd_idle_watch_restart(args: argparse.Namespace, config: Config) -> None:
    """Force-stop existing watcher (if any), then start a new one."""
    target = _idle_validate_ses("--session", args.session)
    stop_args = argparse.Namespace(
        session=target,
        stop_timeout=args.stop_timeout,
        force=True,
    )
    cmd_idle_watch_stop(stop_args, config)
    cmd_idle_watch_start(args, config)


def _add_idle_watch_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register the 5 idle-watch-* subcommands."""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--session",
        required=True,
        help="Target session ID to watch (must start with 'ses').",
    )
    common.add_argument(
        "--message",
        default=None,
        help=(
            "Override the auto-built notify body. When omitted, the watcher sends a "
            "tagged message: '[idle-notify:<reason>] target=<sid>\\n\\nLast assistant "
            "message:\\n```<body>```' (or a stub if the last reply cannot be fetched). "
            "Set this to send arbitrary text instead of the auto-built body."
        ),
    )
    common.add_argument(
        "--interval",
        type=float,
        default=_IDLE_WATCH_INTERVAL_DEFAULT,
        help=f"Poll interval in seconds. Default: {_IDLE_WATCH_INTERVAL_DEFAULT}",
    )
    common.add_argument(
        "--max-poll-seconds",
        type=float,
        default=0,
        help=("Maximum total poll time in seconds before watcher exits regardless of state. 0 = no limit. Default: 0 (infinity)"),
    )
    common.add_argument(
        "--started-at-ms",
        type=int,
        default=0,
        help="Dispatch start timestamp in epoch milliseconds; used with --notify-if-idle-after-update.",
    )
    common.add_argument(
        "--timeout",
        type=float,
        default=_IDLE_WATCH_TIMEOUT_DEFAULT,
        help=(f"Timeout (seconds) for HTTP /status and prompt_async. Default: {_IDLE_WATCH_TIMEOUT_DEFAULT}"),
    )
    common.add_argument(
        "--max-errors",
        type=int,
        default=_IDLE_WATCH_MAX_ERRORS_DEFAULT,
        help=f"Consecutive error limit before fail. Default: {_IDLE_WATCH_MAX_ERRORS_DEFAULT}",
    )
    common.add_argument(
        "--directory",
        default=None,
        help="Optional directory query param for prompt_async.",
    )
    common.add_argument(
        "--workspace",
        default=None,
        help="Optional workspace query param for prompt_async.",
    )
    common.add_argument(
        "--stop-timeout",
        type=float,
        default=_IDLE_WATCH_STOP_TIMEOUT_DEFAULT,
        help=(f"Stop timeout (seconds) after SIGTERM. Default: {_IDLE_WATCH_STOP_TIMEOUT_DEFAULT}"),
    )

    def _add_notify_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--notify-session",
            required=True,
            help="Session ID to notify when target becomes idle (must start with 'ses').",
        )
        parser.add_argument(
            "--continuous",
            action="store_true",
            help="Keep watching after first notify; send on every busy -> idle edge.",
        )
        parser.add_argument(
            "--notify-if-initial-idle",
            action="store_true",
            help="Notify once if target is already 'idle' at first tick.",
        )
        parser.add_argument(
            "--notify-if-idle-after-update",
            action="store_true",
            help="Notify when target is idle and has an assistant reply after --started-at-ms; avoids missing very short dispatched tasks.",
        )

    p = sub.add_parser(
        "idle-watch",
        parents=[common],
        help="Foreground: poll sidecar /status and send prompt_async on busy->idle.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch)

    p = sub.add_parser(
        "idle-watch-start",
        parents=[common],
        help="Background: spawn idle-watch as a detached watcher process.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch_start)

    p = sub.add_parser(
        "idle-watch-stop",
        parents=[common],
        help="Stop background watcher (SIGTERM, escalate to SIGKILL on --force).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Send SIGKILL if process does not exit within --stop-timeout.",
    )
    p.set_defaults(func=cmd_idle_watch_stop)

    p = sub.add_parser(
        "idle-watch-status",
        parents=[common],
        help="Check background watcher state (exit 0 running, 1 no pid, 2 stale).",
    )
    p.set_defaults(func=cmd_idle_watch_status)

    p = sub.add_parser(
        "idle-watch-restart",
        parents=[common],
        help="Force-stop existing watcher, then start a new one.",
    )
    _add_notify_args(p)
    p.set_defaults(func=cmd_idle_watch_restart)


# ---------------- unified command wrappers ----------------


def cmd_service(args: argparse.Namespace, config: Config) -> None:
    """Unified service manager: service <start|stop|status|restart> [opencode|sidecar|all]."""
    action = args.action
    component = args.component

    def _one(name: str, act: str) -> None:
        ns = argparse.Namespace(action=act)
        if name == "opencode":
            cmd_opencode_serve_service(ns, config)
        elif name == "sidecar":
            cmd_sidecar_service(ns, config)
        else:
            fail(f"unknown service component: {name}")

    if action == "status" and component == "all":
        print(
            json.dumps(
                {
                    "opencode": {
                        "healthy": op_healthy(config),
                        "pid": read_pid(pid_file(config, "opencode-server")),
                        "pidFile": str(pid_file(config, "opencode-server")),
                        "logFile": str(log_file(config, "opencode-server")),
                        "url": config.op_server,
                    },
                    "sidecar": {
                        "healthy": sidecar_healthy(config),
                        "pid": read_pid(pid_file(config, "session-status-server")),
                        "pidFile": str(pid_file(config, "session-status-server")),
                        "logFile": str(log_file(config, "session-status-server")),
                        "url": config.sidecar,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if component == "all":
        order = ["opencode", "sidecar"] if action in ("start", "restart") else ["sidecar", "opencode"]
        for name in order:
            _one(name, action)
        return
    _one(component, action)


def cmd_pool_prepare(args: argparse.Namespace, config: Config) -> None:
    cmd_prepare(args, config)


def cmd_pool_release(args: argparse.Namespace, config: Config) -> None:
    cmd_release(args, config)


def cmd_pool_dispatch(args: argparse.Namespace, config: Config) -> None:
    cmd_dispatch(args, config)


def cmd_session_show(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(
        session=args.session_id,
        format=args.format,
        detail=args.detail,
    )
    cmd_overview_session(ns, config)


def cmd_session_status(args: argparse.Namespace, config: Config) -> None:
    sid = args.session_id
    exists = get_session_by_id(config, sid) is not None
    try:
        status_map = http_json("GET", f"{config.sidecar}/status")
    except SystemExit:
        status_map = {}
    state = "unwatch"
    if isinstance(status_map, dict):
        state = str(status_map.get(sid, "unwatch"))
        if state == "unknown":
            state = "unwatch"
    payload = {"sessionID": sid, "exists": exists, "state": state, "tracked": state != "unwatch"}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"sessionID={sid}")
    print(f"exists={str(exists).lower()}")
    print(f"state={state}")
    print(f"tracked={str(state != 'unwatch').lower()}")


def cmd_session_last(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(session=args.session_id, limit=args.limit)
    cmd_last(ns, config)


def cmd_session_dispatch(args: argparse.Namespace, config: Config) -> None:
    ns = argparse.Namespace(
        session=args.session_id,
        wt_id=None,
        agent=args.agent,
        task=args.task,
        yes=args.yes,
        notify_session=args.notify_session,
        require_no_busy=args.require_no_busy,
        max_poll_seconds=args.max_poll_seconds,
    )
    cmd_dispatch(ns, config)


def cmd_session_delete(args: argparse.Namespace, config: Config) -> None:
    sid = args.session_id
    exists = get_session_by_id(config, sid) is not None
    state_touched: list[str] = []
    payload = {
        "dryRun": not args.yes,
        "sessionID": sid,
        "exists": exists,
        "mode": "hard-delete" if args.hard else "soft-delete/tombstone",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.yes:
        print()
        print("确认删除后加 --yes")
        return
    if not args.hard:
        state_touched = _add_tombstone_by_session_id(config, sid)
        if not state_touched:
            eprint(f"warning: no persisted state pointer found for {sid}; soft delete will only unwatch it")
    delete_session(config, sid, hard=args.hard)
    if state_touched:
        eprint(f"tombstoned in state: {', '.join(state_touched)}")
    eprint(f"{'deleted' if args.hard else 'tombstoned'} session: {sid}{' (hard)' if args.hard else ''}")


def cmd_legacy_prepare(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool prepare --branch ..." instead of top-level "prepare".')
    cmd_prepare(args, config)


def cmd_legacy_release(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool release wt_N" instead of top-level "release".')
    cmd_release(args, config)


def cmd_legacy_dispatch(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} pool dispatch wt_N Agent ..." or "{PROG} session dispatch ses_xxx ..." instead of top-level "dispatch".')
    cmd_dispatch(args, config)


def cmd_legacy_status(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        _warn_deprecated(f'use "{PROG} session status {args.session}" instead of top-level "status --session".')
    else:
        _warn_deprecated(f'use "{PROG} service status" for services or "{PROG} overview" for project state.')
    cmd_status(args, config)


def cmd_legacy_last(args: argparse.Namespace, config: Config) -> None:
    _warn_deprecated(f'use "{PROG} session last {args.session}" instead of top-level "last --session".')
    cmd_last(args, config)


def cmd_legacy_overview(args: argparse.Namespace, config: Config) -> None:
    if getattr(args, "session", None):
        _warn_deprecated(f'use "{PROG} session show {args.session}" instead of "overview --session".')
    cmd_overview(args, config)


# ---------------- parser ----------------


def _add_dispatch_options(parser: argparse.ArgumentParser, *, allow_session: bool) -> None:
    if allow_session:
        parser.add_argument("--session", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--task", required=True, help="Task text to send. Without --yes this only previews the prompt.")
    parser.add_argument("--yes", action="store_true", help="Actually send the prompt. Without this flag, print a preview only.")
    parser.add_argument("--force", action="store_true", help="Hard-delete stuck session and create a fresh one before dispatching.")
    parser.add_argument(
        "--notify-session",
        default=env("PM_SESSION_ID", ""),
        help="PM session to notify when target becomes idle. Defaults to $PM_SESSION_ID, then current PM session if available.",
    )
    parser.add_argument(
        "--require-no-busy",
        action="store_true",
        help="Refuse dispatch unless session is idle/unwatch. busy/streaming always fail.",
    )
    parser.add_argument(
        "--max-poll-seconds",
        type=int,
        default=0,
        help="Max poll seconds for auto idle-watch. 0 = unlimited. Recommended: 1800 for long backend tasks.",
    )


def _add_overview_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--detail", action="store_true")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--wt", help="Show one pool worktree, e.g. wt_1 or 1.")
    target_group.add_argument("--main", action="store_true", help="Show main repository sessions only.")
    recent_group = parser.add_mutually_exclusive_group()
    recent_group.add_argument(
        "--recent",
        default=None,
        help=(f"Recent window, e.g. 3d / 24h / 30m / 30s. Default: {_OVERVIEW_RECENT_DEFAULT_SECONDS // 86400}d. Use 0 to keep only the newest session per group."),
    )
    recent_group.add_argument("--all", action="store_true", help="Disable filtering and show every session.")
    parser.add_argument("--show-orphan", action="store_true", help="Include orphan main-worktree sessions. Default: hidden.")
    parser.add_argument("--show-unwatch", action="store_true", help="Include unwatched sessions. Default: hidden.")
    parser.add_argument("--verbose", action="store_true", help="Emit informational warnings to stderr.")


def _add_watch_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", required=True, help="Target session ID to watch, must start with ses.")
    parser.add_argument(
        "--message",
        default=None,
        help="Override notify body. Omit to send tagged last-assistant-message summary.",
    )
    parser.add_argument("--interval", type=float, default=_IDLE_WATCH_INTERVAL_DEFAULT)
    parser.add_argument("--max-poll-seconds", type=float, default=0, help="0 = no limit")
    parser.add_argument("--started-at-ms", type=int, default=0, help="Dispatch start timestamp in epoch milliseconds; used with --notify-if-idle-after-update.")
    parser.add_argument("--timeout", type=float, default=_IDLE_WATCH_TIMEOUT_DEFAULT)
    parser.add_argument("--max-errors", type=int, default=_IDLE_WATCH_MAX_ERRORS_DEFAULT)
    parser.add_argument("--directory", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--stop-timeout", type=float, default=_IDLE_WATCH_STOP_TIMEOUT_DEFAULT)


def _add_watch_notify(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--notify-session", required=True, help="Session ID to notify when target becomes idle.")
    parser.add_argument("--continuous", action="store_true", help="Keep watching after first notify.")
    parser.add_argument("--notify-if-initial-idle", action="store_true", help="Notify if target is idle at first tick.")
    parser.add_argument("--notify-if-idle-after-update", action="store_true", help="Notify if target is idle and has assistant output after --started-at-ms.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenCode worktree/session manager with explicit resource-oriented CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Command model:

  One session by ID:
    {PROG} session show ses_xxx
    {PROG} session status ses_xxx
    {PROG} session last ses_xxx
    {PROG} session dispatch ses_xxx --task "..." --yes
    {PROG} session delete ses_xxx --yes

  Multiple sessions by filters:
    {PROG} sessions list --wt wt_1
    {PROG} sessions list --main
    {PROG} sessions delete --wt wt_1 --agent Daedalus --keep-latest --yes
    {PROG} sessions create --agent Janitor

  Worktree pool lifecycle:
    {PROG} pool init --size 10
    {PROG} pool status --verify
    {PROG} pool repair wt_1
    {PROG} pool prepare --branch feat_xxx
    {PROG} pool dispatch wt_1 Daedalus --task "..." --yes
    {PROG} pool release wt_1

  Services and global inspection:
    {PROG} service status
    {PROG} service start all
    {PROG} overview
    {PROG} overview --wt wt_1
    {PROG} overview --main

Do NOT infer these forms:
  sessions list --session ses_xxx     # wrong; use: session show ses_xxx
  overview --session ses_xxx          # deprecated; use: session show ses_xxx
  status --session ses_xxx            # deprecated; use: session status ses_xxx
""",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{service,pool,session,sessions,overview,watch}")

    # New unified service interface
    service = sub.add_parser(
        "service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage OpenCode and sidecar services.",
        epilog=f"""\
Examples:
  {PROG} service status
  {PROG} service start all
  {PROG} service restart sidecar
  {PROG} service stop all
""",
    )
    service.add_argument("action", choices=["start", "stop", "status", "restart"])
    service.add_argument("component", nargs="?", choices=["opencode", "sidecar", "all"], default="all")
    service.set_defaults(func=cmd_service)

    # Legacy service names, hidden from help but kept for old scripts.
    op_svc = sub.add_parser("opencode-serve-service", help=argparse.SUPPRESS)
    op_svc.add_argument("action", choices=["start", "stop", "status", "restart"])
    op_svc.set_defaults(func=cmd_opencode_serve_service)
    sidecar_svc = sub.add_parser("sidecar-service", help=argparse.SUPPRESS)
    sidecar_svc.add_argument("action", choices=["start", "stop", "status", "restart"])
    sidecar_svc.set_defaults(func=cmd_sidecar_service)

    # Pool resource
    pool = sub.add_parser(
        "pool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage long-lived wt_N worktree pool and pool-owned sessions.",
        epilog=f"""\
Use cases:
  First setup:     {PROG} pool init --size 10
  Health check:    {PROG} pool status --verify
  Repair wt:       {PROG} pool repair wt_1 --reset --force-copy
  Start a task:    {PROG} pool prepare --branch feat_xxx
  Send task:       {PROG} pool dispatch wt_1 Daedalus --task "..." --yes
  Release wt:      {PROG} pool release wt_1 --force
""",
    )
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)

    pool_init = pool_sub.add_parser("init", help="Create/reuse and warm up wt_N pool.")
    pool_init.add_argument("--size", type=int, default=10)
    pool_init.add_argument("--agents")
    pool_init.add_argument("--reset", action="store_true")
    pool_init.add_argument("--force-copy", action="store_true")
    pool_init.set_defaults(func=cmd_pool_init)

    pool_repair = pool_sub.add_parser("repair", help="Repair one wt_N worktree and its sessions.")
    pool_repair.add_argument("wt_id")
    pool_repair.add_argument("--agents")
    pool_repair.add_argument("--reset", action="store_true")
    pool_repair.add_argument("--force-copy", action="store_true")
    pool_repair.set_defaults(func=cmd_pool_repair)

    pool_status = pool_sub.add_parser("status", help="Show pool state. Use --verify to check OpenCode sessions exist.")
    pool_status.add_argument("--size", type=int, default=10)
    pool_status.add_argument("--agents")
    pool_status.add_argument("--verify", action="store_true")
    pool_status.set_defaults(func=cmd_pool_status)

    pool_prepare = pool_sub.add_parser("prepare", help="Grab an idle initialized worktree and check out a task branch.")
    pool_prepare.add_argument("--branch", "-b", required=True)
    pool_prepare.add_argument("--agents")
    pool_prepare.add_argument("--force-branch", action="store_true")
    pool_prepare.set_defaults(func=cmd_pool_prepare)

    pool_dispatch = pool_sub.add_parser("dispatch", help="Dispatch a task to a pool worktree agent session.")
    pool_dispatch.add_argument("wt_id", help="Pool worktree id, e.g. wt_1")
    pool_dispatch.add_argument("agent", help="Agent name, e.g. Daedalus")
    _add_dispatch_options(pool_dispatch, allow_session=False)
    pool_dispatch.set_defaults(func=cmd_pool_dispatch, session=None)

    pool_release = pool_sub.add_parser("release", help="Reset a task worktree and mark it idle.")
    pool_release.add_argument("target", help="wt_N or worktree path")
    pool_release.add_argument("--force", action="store_true", help="Discard uncommitted changes with git reset --hard && git clean -fd.")
    pool_release.set_defaults(func=cmd_pool_release)

    # Single-session resource
    session = sub.add_parser(
        "session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Operate on exactly one OpenCode session by session ID.",
        epilog=f"""\
Examples:
  {PROG} session show ses_xxx
  {PROG} session status ses_xxx
  {PROG} session last ses_xxx
  {PROG} session dispatch ses_xxx --task "..." --yes
  {PROG} session delete ses_xxx --yes

Do not use "sessions list --session". For one session, use "session show/status/last".
""",
    )
    session_sub = session.add_subparsers(dest="session_command", required=True)

    session_show = session_sub.add_parser("show", help="Show one session metadata, tokens, context, and optional last reply.")
    session_show.add_argument("session_id")
    session_show.add_argument("--format", choices=["text", "json"], default="text")
    session_show.add_argument("--detail", action="store_true")
    session_show.set_defaults(func=cmd_session_show)

    session_status = session_sub.add_parser("status", help="Show one session sidecar state: idle/busy/streaming/unwatch.")
    session_status.add_argument("session_id")
    session_status.add_argument("--format", choices=["text", "json"], default="text")
    session_status.set_defaults(func=cmd_session_status)

    session_last = session_sub.add_parser("last", help="Print the last assistant reply for one session.")
    session_last.add_argument("session_id")
    session_last.add_argument("--limit", type=int, default=50)
    session_last.set_defaults(func=cmd_session_last)

    session_dispatch = session_sub.add_parser("dispatch", help="Dispatch a task directly to one session ID.")
    session_dispatch.add_argument("session_id")
    session_dispatch.add_argument("--agent", default=None, help="Optional agent override if session metadata lacks agent.")
    _add_dispatch_options(session_dispatch, allow_session=False)
    session_dispatch.set_defaults(func=cmd_session_dispatch)

    session_delete = session_sub.add_parser("delete", help="Soft-delete/tombstone or hard-delete one session by ID.")
    session_delete.add_argument("session_id")
    session_delete.add_argument("--hard", action="store_true", help="DELETE from OpenCode server. Irreversible.")
    session_delete.add_argument("--yes", action="store_true")
    session_delete.set_defaults(func=cmd_session_delete)

    # Multiple-sessions resource
    sessions_parser = sub.add_parser(
        "sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List/create/delete multiple sessions by explicit filters. Use 'session ...' for one ID.",
        epilog=f"""\
Examples:
  {PROG} sessions create --agent Janitor
  {PROG} sessions list --wt wt_1
  {PROG} sessions list --main --agent Janitor
  {PROG} sessions delete --wt wt_1 --agent Daedalus --keep-latest --yes

Wrong:
  {PROG} sessions list --session ses_xxx
Right:
  {PROG} session show ses_xxx
""",
    )
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command", required=True)

    sessions_create = sessions_sub.add_parser("create", help="Create/reuse a persistent main-repo agent session.")
    sessions_create.add_argument("--agent", required=True, help="Agent name, e.g. Janitor, General, Momus, Clio.")
    sessions_create.add_argument(
        "--force",
        action="store_true",
        help="Hard-delete persisted existing session and create a new one. Without --force, stale/oversized sessions are archived/unwatched.",
    )
    sessions_create.add_argument("--directory", default=None, help="Directory for the session. Default: repo root.")
    sessions_create.set_defaults(func=cmd_session_create)

    sessions_list = sessions_sub.add_parser("list", help="List sessions by --wt/--main/--path filters.")
    sessions_list.add_argument("target", nargs="?", help=argparse.SUPPRESS)  # legacy positional target
    sessions_list.add_argument("--wt", help="Worktree id, e.g. wt_1 or 1.")
    sessions_list.add_argument("--main", action="store_true", help="Main repository sessions.")
    sessions_list.add_argument("--path", help="Explicit worktree path.")
    sessions_list.add_argument("--session", help=argparse.SUPPRESS)
    sessions_list.add_argument("--agent")
    sessions_list.add_argument("--agents")
    sessions_list.add_argument("--format", choices=["text", "json"], default="text")
    sessions_list.set_defaults(func=cmd_sessions_list)

    sessions_delete = sessions_sub.add_parser("delete", help="Delete/tombstone sessions by --wt/--main/--path filters.")
    sessions_delete.add_argument("target", nargs="?", help=argparse.SUPPRESS)  # legacy positional target
    sessions_delete.add_argument("--wt", help="Worktree id, e.g. wt_1 or 1.")
    sessions_delete.add_argument("--main", action="store_true", help="Main repository sessions.")
    sessions_delete.add_argument("--path", help="Explicit worktree path.")
    sessions_delete.add_argument("--session", help=argparse.SUPPRESS)
    sessions_delete.add_argument("--agent")
    sessions_delete.add_argument("--agents")
    sessions_delete.add_argument("--keep-latest", action="store_true")
    sessions_delete.add_argument("--hard", action="store_true", help="DELETE from OpenCode server. Irreversible.")
    sessions_delete.add_argument("--yes", action="store_true")
    sessions_delete.set_defaults(func=cmd_sessions_delete)

    # Global overview
    overview = sub.add_parser(
        "overview",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Global/session landscape overview. For one session, prefer: session show ses_xxx.",
        epilog=f"""\
Examples:
  {PROG} overview
  {PROG} overview --wt wt_1
  {PROG} overview --main
  {PROG} overview --all

Deprecated compatibility:
  {PROG} overview --session ses_xxx   # use: {PROG} session show ses_xxx
""",
    )
    _add_overview_filters(overview)
    overview.add_argument("--session", help=argparse.SUPPRESS)
    overview.set_defaults(func=cmd_legacy_overview)

    # Watch resource (new names), backed by existing idle-watch implementation.
    watch = sub.add_parser(
        "watch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage idle-watch processes.",
        epilog=f"""\
Examples:
  {PROG} watch idle --session ses_target --notify-session ses_pm
  {PROG} watch start --session ses_target --notify-session ses_pm
  {PROG} watch status
  {PROG} watch status --session ses_target
  {PROG} watch status --watcher-process --session ses_target
  {PROG} watch stop --session ses_target --force
""",
    )
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)

    watch_idle = watch_sub.add_parser("idle", help="Foreground one-shot/continuous busy->idle watcher.")
    _add_watch_common(watch_idle)
    _add_watch_notify(watch_idle)
    watch_idle.set_defaults(func=cmd_idle_watch)

    watch_start = watch_sub.add_parser("start", help="Start a detached idle watcher.")
    _add_watch_common(watch_start)
    _add_watch_notify(watch_start)
    watch_start.set_defaults(func=cmd_idle_watch_start)

    watch_stop = watch_sub.add_parser("stop", help="Stop a detached idle watcher.")
    _add_watch_common(watch_stop)
    watch_stop.add_argument("--force", action="store_true")
    watch_stop.set_defaults(func=cmd_idle_watch_stop)

    watch_status = watch_sub.add_parser("status", help="Show sidecar watch status; optionally inspect detached watcher process.")
    watch_status.add_argument("--detail", action="store_true", help="Show detailed sidecar session status, same as top-level status --detail.")
    watch_status.add_argument("--session", help="Show sidecar status for one session, same as top-level status --session.")
    watch_status.add_argument("--watcher-process", action="store_true", help="With --session, check detached idle-watch process pid/log status instead of sidecar status.")
    watch_status.set_defaults(func=cmd_watch_status)

    watch_restart = watch_sub.add_parser("restart", help="Restart detached idle watcher.")
    _add_watch_common(watch_restart)
    _add_watch_notify(watch_restart)
    watch_restart.set_defaults(func=cmd_idle_watch_restart)

    # Legacy top-level aliases, hidden from help but kept compatible.
    prepare = sub.add_parser("prepare", help=argparse.SUPPRESS)
    prepare.add_argument("--branch", "-b", required=True)
    prepare.add_argument("--agents")
    prepare.add_argument("--force-branch", action="store_true")
    prepare.set_defaults(func=cmd_legacy_prepare)

    dispatch = sub.add_parser("dispatch", help=argparse.SUPPRESS)
    dispatch.add_argument("wt_id", nargs="?")
    dispatch.add_argument("agent", nargs="?")
    dispatch.add_argument("--session", default=None)
    _add_dispatch_options(dispatch, allow_session=False)
    dispatch.set_defaults(func=cmd_legacy_dispatch)

    release = sub.add_parser("release", help=argparse.SUPPRESS)
    release.add_argument("target")
    release.add_argument("--force", action="store_true")
    release.set_defaults(func=cmd_legacy_release)

    status = sub.add_parser("status", help=argparse.SUPPRESS)
    status.add_argument("--detail", action="store_true")
    status.add_argument("--session")
    status.set_defaults(func=cmd_legacy_status)

    last = sub.add_parser("last", help=argparse.SUPPRESS)
    last.add_argument("--session", required=True)
    last.add_argument("--limit", type=int, default=50)
    last.set_defaults(func=cmd_legacy_last)

    # Legacy idle-watch top-level aliases.
    _add_idle_watch_subparsers(sub)

    # Hide deprecated compatibility aliases from top-level help while keeping
    # them callable. argparse.SUPPRESS alone still leaks as "==SUPPRESS=="
    # in some Python versions for subparser pseudo-actions, so prune the
    # display list explicitly and use a fixed metavar above for usage.
    legacy_names = {
        "opencode-serve-service",
        "sidecar-service",
        "prepare",
        "dispatch",
        "release",
        "status",
        "last",
        "idle-watch",
        "idle-watch-start",
        "idle-watch-stop",
        "idle-watch-status",
        "idle-watch-restart",
    }
    sub._choices_actions = [a for a in sub._choices_actions if getattr(a, "dest", "") not in legacy_names and getattr(a, "help", None) is not argparse.SUPPRESS]
    return parser


def main() -> None:
    # Build/parse CLI before loading repo-bound config so ``-h`` works from
    # anywhere. This is important for AI self-correction after a failed call.
    parser = build_parser()
    args = parser.parse_args()
    config = Config.load()
    args.func(args, config)


if __name__ == "__main__":
    main()
