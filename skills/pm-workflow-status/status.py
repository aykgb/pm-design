#!/usr/bin/env python3
"""
status.py — PM Workflow S(tatus) 的 Python 实现

对应 skill：`.opencode/skills/pm-workflow-status/SKILL.md`
对应 SKILL.md 「项目体检清单」一节的 9 项检查。

本脚本作为 PM 文档的一部分，与 SKILL.md 同目录共存。
仅读取仓库状态，不修改任何文件；不依赖项目应用代码（无 DB / 无 ORM / 无 QMT）。

用法：

    python .opencode/skills/pm-workflow-status/status.py                           # 人读
    python .opencode/skills/pm-workflow-status/status.py --json                    # 机器可读
    python .opencode/skills/pm-workflow-status/status.py --quiet                   # 仅 fail 时输出
    python .opencode/skills/pm-workflow-status/status.py --pm-session-id ses_xxx   # 比对 guard block
    python .opencode/skills/pm-workflow-status/status.py --help                    # 帮助

退出码：

    0 = 全 pass（或仅 warn / skip）
    1 = 至少 1 项 fail
    2 = 参数错误
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# ============================================================
# Repo root discovery
# ============================================================


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` looking for .git or pyproject.toml."""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            return parent
    return cur


REPO_ROOT = find_repo_root(Path(__file__).parent)


# ============================================================
# Check result model
# ============================================================


@dataclass
class CheckResult:
    name: str  # 检查项 ID（与 SKILL.md 体检清单行名一致）
    status: str  # "pass" | "warn" | "fail" | "skip"
    expected: str  # 期望值（人读）
    actual: str  # 实测值（人读）


# ============================================================
# Git helpers
# ============================================================


def run_git(*args: str) -> str:
    """Run git and return stdout (stripped). Empty on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return ""


# ============================================================
# Checks (1:1 with SKILL.md 项目体检清单)
# ============================================================


def check_worktree_clean() -> CheckResult:
    out = run_git("status", "--short")
    dirty = len([line for line in out.splitlines() if line])
    return CheckResult(
        name="worktree_clean",
        status="pass" if dirty == 0 else "warn",
        expected="empty",
        actual=f"{dirty} dirty entries" if dirty else "(clean)",
    )


def check_branch() -> CheckResult:
    branch = run_git("branch", "--show-current")
    return CheckResult(
        name="branch",
        status="pass" if branch == "main" else "warn",
        expected="main",
        actual=branch or "(detached HEAD)",
    )


def check_untracked() -> CheckResult:
    out = run_git("ls-files", "--others", "--exclude-standard")
    count = len([line for line in out.splitlines() if line])
    return CheckResult(
        name="untracked_files",
        status="pass" if count == 0 else "warn",
        expected="0",
        actual=str(count),
    )


def check_python_source() -> CheckResult:
    """Python source LOC（除 .opencode/、scripts/、__pycache__/、main.py stub 之外）"""
    src_files: list[Path] = []
    for p in REPO_ROOT.rglob("*.py"):
        rel = p.relative_to(REPO_ROOT)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue
        if "__pycache__" in parts:
            continue
        if p.name == "main.py":
            continue
        if "scripts" in parts:
            continue
        src_files.append(p)
    total_loc = 0
    for p in src_files:
        try:
            with p.open(encoding="utf-8") as f:
                total_loc += sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            pass
    return CheckResult(
        name="python_source",
        status="pass" if total_loc > 0 else "warn",
        expected="> 0 (V1 早期可为空)",
        actual=f"{total_loc} LOC across {len(src_files)} files",
    )


def check_test_files() -> CheckResult:
    test_files = [p for p in REPO_ROOT.rglob("test_*.py") if not any(part.startswith(".") for part in p.relative_to(REPO_ROOT).parts)]
    return CheckResult(
        name="test_files",
        status="pass" if test_files else "warn",
        expected=">= Active TASK Type=Test count",
        actual=f"{len(test_files)} test file(s)",
    )


def check_db_migration() -> CheckResult:
    db_dir = REPO_ROOT / "database"
    sql_files = list(db_dir.glob("*.sql")) if db_dir.exists() else []
    return CheckResult(
        name="db_migration",
        status="pass" if sql_files else "fail",
        expected=">= 1 .sql file in database/",
        actual=f"{len(sql_files)} .sql file(s)",
    )


def check_runtime_config_defaults() -> CheckResult:
    """runtime_config 安全默认值（CLAUDE.md §3）—— 通过 SQL 文本检查"""
    sql_path = REPO_ROOT / "database" / "001_init_minimal_trading_system.sql"
    if not sql_path.exists():
        return CheckResult(
            name="runtime_config_defaults",
            status="skip",
            expected="trading_enabled=false, dry_run_mode=true, order_submit_enabled=false",
            actual="(migration file not found)",
        )
    text = sql_path.read_text(encoding="utf-8")
    checks = {
        "trading_enabled=false": "'trading_enabled', 'false'::jsonb" in text,
        "dry_run_mode=true": "'dry_run_mode', 'true'::jsonb" in text,
        "order_submit_enabled=false": "'order_submit_enabled', 'false'::jsonb" in text,
    }
    all_ok = all(checks.values())
    detail = ", ".join(f"{k}={'✔' if v else '✘'}" for k, v in checks.items())
    return CheckResult(
        name="runtime_config_defaults",
        status="pass" if all_ok else "fail",
        expected="trading_enabled=false, dry_run_mode=true, order_submit_enabled=false",
        actual=detail,
    )


def check_pm_agent_alignment() -> CheckResult:
    """PM agent 是否含 CLAUDE.md 对齐声明（PM agent §CLAUDE.md 对齐声明）"""
    pm_path = REPO_ROOT / ".opencode" / "agents" / "project-manager.md"
    if not pm_path.exists():
        return CheckResult(
            name="pm_agent_alignment",
            status="fail",
            expected="contains 'CLAUDE.md 对齐声明'",
            actual="(file not found)",
        )
    text = pm_path.read_text(encoding="utf-8")
    aligned = "CLAUDE.md 对齐声明" in text
    return CheckResult(
        name="pm_agent_alignment",
        status="pass" if aligned else "fail",
        expected="contains 'CLAUDE.md 对齐声明'",
        actual="(found)" if aligned else "(missing)",
    )


def check_pm_session_active() -> CheckResult:
    """验证 pm-session-info.json 指向本 PM 会话（未被 guard block 到旧会话）"""
    info_path = REPO_ROOT / ".pm" / "pm-session-info.json"
    if not info_path.exists():
        return CheckResult(
            name="pm_session_active",
            status="fail",
            expected="pm-session-info.json exists",
            actual="(file not found)",
        )
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return CheckResult(
            name="pm_session_active",
            status="fail",
            expected="valid JSON",
            actual="(unreadable)",
        )
    file_sid = str(data.get("current_session_id") or "")
    if not _pm_session_id:
        return CheckResult(
            name="pm_session_active",
            status="warn",
            expected="--pm-session-id 传入本会话 ID 后可自动比对",
            actual=f"session={file_sid[:20]}... (未传入，跳过比对)",
        )
    if file_sid == _pm_session_id:
        return CheckResult(
            name="pm_session_active",
            status="pass",
            expected=f"current_session_id = {_pm_session_id[:20]}...",
            actual="match",
        )
    return CheckResult(
        name="pm_session_active",
        status="fail",
        expected=f"current_session_id = {_pm_session_id[:20]}...",
        actual=f"current_session_id = {file_sid[:20]}... (guard blocked — takeover needed)",
    )


# 注册表：顺序与 SKILL.md 体检清单一致
ALL_CHECKS: list[Callable[[], CheckResult]] = [
    check_worktree_clean,
    check_branch,
    check_untracked,
    check_python_source,
    check_test_files,
    check_db_migration,
    check_runtime_config_defaults,
    check_pm_agent_alignment,
    check_pm_session_active,
]


SYMBOL = {"pass": "✔", "warn": "⚠", "fail": "✘", "skip": "⏭"}

# 模块级变量：由 CLI --pm-session-id 传入，供 check_pm_session_active 比对
_pm_session_id: str = ""


# ============================================================
# Renderers
# ============================================================


def collect_checks() -> list[CheckResult]:
    return [fn() for fn in ALL_CHECKS]


def render_human(checks: list[CheckResult]) -> str:
    lines = ["# CI 健康度总览", ""]
    for c in checks:
        sym = SYMBOL.get(c.status, "?")
        lines.append(f"- {sym} **{c.name}** — expected: {c.expected}; actual: {c.actual}")
    fail = sum(1 for c in checks if c.status == "fail")
    warn = sum(1 for c in checks if c.status == "warn")
    skip = sum(1 for c in checks if c.status == "skip")
    pass_ = sum(1 for c in checks if c.status == "pass")
    lines.append("")
    lines.append(f"总计：{len(checks)} 项 / pass={pass_} / warn={warn} / fail={fail} / skip={skip}")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PM Workflow S(tatus) — 项目健康度快速扫描（9 项检查）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--quiet", action="store_true", help="仅在有 fail 时输出")
    parser.add_argument("--pm-session-id", default="", help="当前 PM 会话 ID（用于比对 pm-session-info.json 是否被 guard block）")
    args = parser.parse_args(argv)

    global _pm_session_id
    _pm_session_id = args.pm_session_id

    checks = collect_checks()
    fail_count = sum(1 for c in checks if c.status == "fail")

    if args.json:
        print(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2))
    elif args.quiet and fail_count == 0:
        pass  # quiet + no fail → 静默
    else:
        print(render_human(checks))

    if fail_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
