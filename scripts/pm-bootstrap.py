#!/usr/bin/env python3
"""PM System Bootstrap — 从 .pm/design/templates/ 生成 PM 域文件。

用法：
    python scripts/pm-bootstrap.py                              # 交互式（默认读 pm.config.yaml）
    python scripts/pm-bootstrap.py --from docs/                 # 从项目文档推断配置
    python scripts/pm-bootstrap.py --config ~/.pm/myconf.yaml   # 自定义配置路径
    python scripts/pm-bootstrap.py --dry-run                    # 预览，不写入

前提：.pm/design/ 已作为 git submodule 就位。
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_PM = DESIGN_DIR / "templates" / "pm"
TEMPLATES_AGENTS = DESIGN_DIR / "templates" / "agents"
SKILLS_DIR = DESIGN_DIR / "skills"
RUNTIME_DIR = DESIGN_DIR / "runtime"
CONFIG_FILE = PROJECT_ROOT / "pm.config.yaml"

# ——— placeholder substitution ———


def substitute(text: str, vars_: dict[str, str]) -> str:
    """Replace {{key}} placeholders with values."""
    for key, val in vars_.items():
        text = text.replace(f"{{{{{key}}}}}", val)
    return text


# ——— config discovery ———


def read_config() -> dict[str, str]:
    """Read pm.config.yaml or return defaults inferred from project docs."""
    config: dict[str, str] = {
        "project_name": "MyProject",
        "dev_agent": "DevAgent",
        "review_agent": "ReviewAgent",
        "qa_agent": "QAAgent",
        "gate_agent": "SpecGate",
        "branch_prefix": "feat_P",
    }

    if CONFIG_FILE.exists():
        import yaml  # lazy import

        with open(CONFIG_FILE) as f:
            yml = yaml.safe_load(f)
        if yml:
            if "project" in yml and "name" in yml["project"]:
                config["project_name"] = yml["project"]["name"]
            if "agents" in yml:
                agents = yml["agents"]
                config["dev_agent"] = agents.get("dev", config["dev_agent"])
                config["review_agent"] = agents.get("review", config["review_agent"])
                config["qa_agent"] = agents.get("qa", config["qa_agent"])
                config["gate_agent"] = agents.get("gate", config["gate_agent"])
            if "conventions" in yml and "branch_prefix" in yml["conventions"]:
                config["branch_prefix"] = yml["conventions"]["branch_prefix"]

    return config


def infer_from_plan() -> dict[str, str]:
    """Infer project metadata from implementation_plan.md."""
    config = read_config()
    plan = PROJECT_ROOT / "docs" / "implementation_plan.md"
    if not plan.exists():
        return config

    text = plan.read_text(encoding="utf-8")
    # Try to find project name: first # heading or "项目名称："
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if m and config["project_name"] == "MyProject":
        name = m.group(1).strip()
        # Strip common suffixes
        name = re.sub(r"\s*(开发计划|实施计划|Implementation Plan).*", "", name)
        if name:
            config["project_name"] = name

    # Detect first incomplete phase
    phases = re.findall(r"Phase\s+(\d+)[：:]\s*(.+)$", text, re.MULTILINE)
    incomplete = None
    for num, name in phases:
        if "✅" not in name and "完成" not in name:
            incomplete = f"Phase {num} — {name.strip()}"
            break
    if incomplete:
        config["current_phase"] = incomplete

    return config


# ——— file generation ———


def generate_file(
    template_path: Path,
    target_path: Path,
    config: dict[str, str],
    dry_run: bool,
) -> bool:
    """Copy template to target, substituting placeholders. Returns True if created."""
    if not template_path.exists():
        print(f"   ⚠ 模板不存在: {template_path}")
        return False
    if target_path.exists():
        print(f"   ⏭ 已存在，跳过: {target_path}")
        return False

    content = template_path.read_text(encoding="utf-8")
    content = substitute(content, config)

    if dry_run:
        print(f"   📄 将创建: {target_path} ({len(content)} chars)")
        return True

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    print(f"   ✅ 创建: {target_path}")
    return True


def generate_project_tasks(config: dict[str, str], dry_run: bool) -> bool:
    """Generate docs/project_tasks.md from implementation_plan.md."""
    target = PROJECT_ROOT / "docs" / "project_tasks.md"
    if target.exists():
        print(f"   ⏭ 已存在，跳过: {target}")
        return False

    plan = PROJECT_ROOT / "docs" / "implementation_plan.md"
    if not plan.exists():
        print("   ⚠ 无 implementation_plan.md，跳过 project_tasks.md")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    phase_info = config.get("current_phase", "Phase 0 — 待定义")

    content = f"""# Project TASK

Last updated: {now}

> 任务完成历史见 [`development_log.md`](development_log.md)
>
> 详细 spec 见 [`docs/task_specs/`](task_specs/)
>
> 当前仓库状态：初始化完成，待首批 Spec 拆解

## Current Phase

- **Phase**: {phase_info}
- **Source plan**: `docs/implementation_plan.md`
- **Status**: 待 Spec 拆解
- **Tests**: 0
- **Branch**: `main`（clean）

## Active TASK

| 任务 | 状态 | 优先级 | 类型 | Spec | 执行顺序 |
| --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | 待 Workflow N 拆解 |

## Backlog / Later

- 初始化完成，暂无 Backlog

## Recently Completed

- keep latest 6 items.

| Date | Task | Commit | Summary |
| --- | --- | --- | --- |
"""

    if dry_run:
        print(f"   📄 将创建: {target} ({len(content)} chars)")
        return True

    target.write_text(content, encoding="utf-8")
    print(f"   ✅ 创建: {target}")
    return True


def generate_devlog(dry_run: bool) -> bool:
    """Generate empty docs/development_log.md."""
    target = PROJECT_ROOT / "docs" / "development_log.md"
    if target.exists():
        return False

    content = """# Development Log

> 开发历史。每完成一轮工作在此表顶部插入一行。

| Date | Slug | Summary | PR / Commits |
| --- | --- | --- | --- |
"""

    if dry_run:
        print(f"   📄 将创建: {target} ({len(content)} chars)")
        return True

    target.write_text(content, encoding="utf-8")
    print(f"   ✅ 创建: {target}")
    return True


def generate_pm_config(config: dict[str, str], dry_run: bool) -> bool:
    """Generate pm.config.yaml if it doesn't exist."""
    if CONFIG_FILE.exists():
        return False

    content = f"""# PM System Configuration
# 自动生成于 bootstrap。手动微调后 pm bootstrap --from docs/ 不会覆盖已有值。

project:
  name: "{config["project_name"]}"

agents:
  dev: {config["dev_agent"]}
  review: {config["review_agent"]}
  qa: {config["qa_agent"]}
  gate: {config["gate_agent"]}          # 可选：Spec 门禁

conventions:
  branch_prefix: {config["branch_prefix"]}
  review_required: true
  tools_dir: scripts/

phases:
  source: docs/implementation_plan.md
"""

    if dry_run:
        print(f"   📄 将创建: {CONFIG_FILE} ({len(content)} chars)")
        return True

    CONFIG_FILE.write_text(content, encoding="utf-8")
    print(f"   ✅ 创建: {CONFIG_FILE}")
    return True


def copy_skills(dry_run: bool) -> tuple[int, int]:
    """Copy pm-workflow-* skills from pm-design to project .opencode/skills/. Returns (copied, skipped)."""
    if not SKILLS_DIR.exists():
        return 0, 0

    copied, skipped = 0, 0
    target_base = PROJECT_ROOT / ".opencode" / "skills"

    for src in sorted(SKILLS_DIR.iterdir()):
        if not src.is_dir():
            continue
        dst = target_base / src.name
        if dst.exists():
            skipped += 1
            continue
        if dry_run:
            print(f"   📁 将复制 skill: {src.name}")
        else:
            _copy_dir(src, dst)
            print(f"   ✅ 复制 skill: {src.name}")
        copied += 1

    return copied, skipped


def copy_runtime_scripts(dry_run: bool) -> tuple[int, int]:
    """Copy runtime scripts (session-worktree-mgr.py, etc.) from pm-design to project scripts/. Returns (copied, skipped)."""
    if not RUNTIME_DIR.exists():
        return 0, 0

    copied, skipped = 0, 0
    target_base = PROJECT_ROOT / "scripts"

    for src in RUNTIME_DIR.iterdir():
        if not src.is_file() or src.name.startswith("."):
            continue
        dst = target_base / src.name
        if dst.exists():
            skipped += 1
            continue
        if dry_run:
            print(f"   📄 将复制 runtime: {src.name}")
        else:
            target_base.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, dst)
            print(f"   ✅ 复制 runtime: {src.name}")
        copied += 1

    return copied, skipped


def _copy_dir(src: Path, dst: Path) -> None:
    """Recursively copy a directory, creating parent dirs."""
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


# ——— main ———


def main() -> None:
    parser = argparse.ArgumentParser(description="PM System Bootstrap")
    parser.add_argument("--from", dest="from_dir", default=None, help="从项目文档推断配置")
    parser.add_argument("--config", dest="config", default=None, help="自定义 pm.config.yaml 路径（默认：项目根目录）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入文件")
    args = parser.parse_args()

    # 支持自定义配置文件路径
    if args.config:
        global CONFIG_FILE
        CONFIG_FILE = Path(args.config).expanduser().resolve()
        if not CONFIG_FILE.parent.exists():
            print(f"❌ 配置目录不存在: {CONFIG_FILE.parent}")
            sys.exit(1)

    if not DESIGN_DIR.exists():
        print("❌ .pm/design/ 不存在。请先添加 submodule：")
        print("   git submodule add <url> .pm/design")
        sys.exit(1)

    # Discover config
    config = infer_from_plan() if args.from_dir else read_config()

    print(f"\n🔧 PM Bootstrap — {config['project_name']}")
    print(f"   配置: {CONFIG_FILE}")
    print(f"   Agent: dev={config['dev_agent']} review={config['review_agent']} qa={config['qa_agent']}")
    if args.dry_run:
        print("   (dry-run 模式，不写入文件)\n")
    else:
        print()

    generated = 0
    skipped = 0

    # PM templates
    for name in ["project_memory.md", "operational_conventions.md", "persona.md"]:
        tpl = TEMPLATES_PM / name
        tgt = PROJECT_ROOT / ".pm" / name
        if generate_file(tpl, tgt, config, args.dry_run):
            generated += 1
        elif tgt.exists():
            skipped += 1

    # CLAUDE.md（项目根目录）
    claude_tpl = TEMPLATES_PM / "CLAUDE.md"
    claude_tgt = PROJECT_ROOT / "CLAUDE.md"
    if generate_file(claude_tpl, claude_tgt, config, args.dry_run):
        generated += 1
    elif claude_tgt.exists():
        skipped += 1

    # Agent templates
    for key, tpl_name in [
        ("dev_agent", "dev.agent.md"),
        ("review_agent", "review.agent.md"),
        ("qa_agent", "qa.agent.md"),
    ]:
        tpl = TEMPLATES_AGENTS / tpl_name
        agent_name = config[key]
        tgt = PROJECT_ROOT / ".opencode" / "agents" / f"{agent_name.lower()}.md"
        if generate_file(tpl, tgt, config, args.dry_run):
            generated += 1
        elif tgt.exists():
            skipped += 1

    # Special files
    for fn in [generate_pm_config, generate_project_tasks, generate_devlog]:
        args_list = [args.dry_run]
        if fn is not generate_devlog:
            args_list.insert(0, config)
            if fn(config, args.dry_run):
                generated += 1
        else:
            if fn(args.dry_run):
                generated += 1

    # Copy skills from pm-design
    sc, ss = copy_skills(args.dry_run)
    generated += sc
    skipped += ss

    # Copy runtime scripts from pm-design
    rc, rs = copy_runtime_scripts(args.dry_run)
    generated += rc
    skipped += rs

    # Ensure directories
    for d in [".pm/chats", ".pm/reflections", "docs/task_specs"]:
        p = PROJECT_ROOT / d
        if not p.exists():
            if args.dry_run:
                print(f"   📁 将创建目录: {p}")
            else:
                p.mkdir(parents=True, exist_ok=True)
                print(f"   📁 创建目录: {p}")

    # Ensure .pm/chats/INDEX.md
    idx = PROJECT_ROOT / ".pm" / "chats" / "INDEX.md"
    if not idx.exists():
        if args.dry_run:
            print(f"   📄 将创建: {idx}")
        else:
            idx.parent.mkdir(parents=True, exist_ok=True)
            idx.write_text(
                "# 闲聊索引\n\n| 日期 | 时长 | 关键话题 | 文件 |\n| --- | --- | --- | --- |\n",
                encoding="utf-8",
            )
            print(f"   ✅ 创建: {idx}")

    print()
    if args.dry_run:
        print("✅ Dry-run 完成。去掉 --dry-run 执行实际写入。")
    else:
        print(f"✅ Bootstrap 完成：{generated} 个文件已创建，{skipped} 个已存在跳过。")
        print("   下一步：开发者说 '下一步' → PM 启动 Workflow N")


if __name__ == "__main__":
    main()
