#!/usr/bin/env bash
# check-codex.sh — 拉取 PR 上 Codex bot 的审查 comments
#
# 用法：
#   bash scripts/check-codex.sh <PR_NUMBER>                  # 输出 Markdown
#   bash scripts/check-codex.sh <PR_NUMBER> --json           # 输出 JSON
#   bash scripts/check-codex.sh <PR_NUMBER> --count          # 只输出计数
#   bash scripts/check-codex.sh <PR_NUMBER> --repo OWNER/REPO  # 显式指定 repo (默认从 git remote 推断)
#
# 由 Themis 在审查流程 Step 1.5 调用，PM 也可手工运行。
#
# 跨项目支持 (per devkit review B2 修复): repo 默认从 `git remote get-url origin`
# 推断; 也可通过 --repo 参数或 PM_CODEX_REPO 环境变量显式指定.
# 部署到新项目时无需改 hardcoded `aykgb/xidi-minimal`.

set -euo pipefail

PR_NUMBER="${1:-}"
FLAG="${2:---markdown}"
REPO_OVERRIDE=""

# 解析参数: 第 1 个是 PR_NUMBER, 第 2 个可能是 FLAG 或 --repo OWNER/REPO
# 支持 `check-codex.sh <PR> --repo OWNER/REPO` 或 `check-codex.sh <PR> --markdown --repo OWNER/REPO`
shift_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO_OVERRIDE="$2"
            shift 2
            ;;
        --json|--markdown|--count)
            shift_args+=("$1")
            shift
            ;;
        *)
            shift_args+=("$1")
            shift
            ;;
    esac
done
# 重建位置参数
set -- "${shift_args[@]:-}"
PR_NUMBER="${1:-}"
FLAG="${2:---markdown}"

if [[ -z "$PR_NUMBER" ]]; then
    echo "用法: bash scripts/check-codex.sh <PR_NUMBER> [--json|--markdown|--count] [--repo OWNER/REPO]" >&2
    exit 2
fi

# 推断 REPO: --repo > PM_CODEX_REPO 环境变量 > git remote get-url origin
if [[ -n "$REPO_OVERRIDE" ]]; then
    REPO="$REPO_OVERRIDE"
elif [[ -n "${PM_CODEX_REPO:-}" ]]; then
    REPO="$PM_CODEX_REPO"
else
    # 从 git remote 推断 OWNER/REPO
    REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [[ -z "$REMOTE_URL" ]]; then
        echo "❌ 无法推断 repo: 未传 --repo、未设 PM_CODEX_REPO、且无 git remote origin" >&2
        echo "   用法: bash scripts/check-codex.sh <PR> --repo OWNER/REPO" >&2
        exit 2
    fi
    # 支持 SSH (git@github.com:OWNER/REPO.git) 和 HTTPS (https://github.com/OWNER/REPO.git)
    REPO=$(echo "$REMOTE_URL" | sed -E 's#^(git@github\.com:|https?://github\.com/)##; s#\.git$##')
    if [[ -z "$REPO" ]] || [[ "$REPO" == *"@"* ]]; then
        echo "❌ 无法从 remote URL 解析 OWNER/REPO: $REMOTE_URL" >&2
        echo "   用法: bash scripts/check-codex.sh <PR> --repo OWNER/REPO" >&2
        exit 2
    fi
fi

# 拉取 PR review comments
COMMENTS_JSON=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}/comments" 2>/dev/null || true)

if [[ -z "$COMMENTS_JSON" ]] || [[ "$COMMENTS_JSON" == "[]" ]]; then
    case "$FLAG" in
        --json)   echo '{"codex_findings": [], "total": 0}' ;;
        --count)  echo "0" ;;
        *)        echo "_Codex bot: 无 comments（PR #${PR_NUMBER} 尚无 Codex 审查）。_" ;;
    esac
    exit 0
fi

# 提取 Codex bot 的 comments（过滤掉 Themis / Daedalus 等人的 comments）
CODEX_COMMENTS=$(echo "$COMMENTS_JSON" | python3 -c "
import json, sys, re

data = json.load(sys.stdin)
codex = []
for c in data:
    login = c.get('user', {}).get('login', '')
    if 'codex' in login.lower():
        body = c.get('body', '')
        # 去掉 badge 图片
        body = re.sub(r'!\[.*?\]\(.*?\)', '', body)
        body = re.sub(r'\*\*<sub><sub>.*?</sub></sub>\*\*', '', body)
        body = re.sub(r'Useful\? React with.*', '', body)
        body = body.strip()
        codex.append({
            'path': c.get('path', ''),
            'line': c.get('line', 0),
            'body': body,
            'id': c.get('id', ''),
        })
print(json.dumps(codex, ensure_ascii=False))
")

TOTAL=$(echo "$CODEX_COMMENTS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")

case "$FLAG" in
    --json)
        echo "{\"codex_findings\": $CODEX_COMMENTS, \"total\": $TOTAL}"
        ;;
    --count)
        echo "$TOTAL"
        ;;
    *)
        if [[ "$TOTAL" -eq 0 ]]; then
            echo "_Codex bot: 无审查 comments（PR #${PR_NUMBER}）。_"
        else
            echo "## Codex Bot Findings（PR #${PR_NUMBER}，${TOTAL} 条）"
            echo ""
            echo "$CODEX_COMMENTS" | python3 -c "
import json, sys
for i, c in enumerate(json.load(sys.stdin), 1):
    loc = f'{c[\"path\"]}:{c[\"line\"]}' if c['path'] else ''
    body = c['body'].replace('\n', ' ')
    print(f'- [{loc}]({loc}) {body}')
"
        fi
        ;;
esac
