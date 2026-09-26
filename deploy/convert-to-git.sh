#!/bin/bash
# convert-to-git.sh —— 一次性：把手工 rsync 部署的生产目录原地变成 git 工作区。
#
#   deploy/convert-to-git.sh v0.1.0 [--repo URL] [--app DIR] [--allow 路径]...
#
# 前提：目标 tag 与生产上正在跑的代码**逐字节一致**（2026-09-26 核实：生产 =
# 2baa0c3 = v0.1.0）。转换不改变任何运行行为，只是让生产目录"知道自己是哪个版本"；
# 升级走之后的 deploy.sh <tag>。可逆：rm -rf .git 再解开转换前的快照即可（结尾会打印）。
#
# 生产目录本身在哪就留在哪：config.py 等用 Path(__file__).resolve() 定位项目根，
# state/ 里的审计又存着绝对路径（deploy 调研 §5），所以不搬家、不做 releases/ 软链。
#
# 步骤：快照（不含 state/ 与 .venv）→ git clone --no-checkout 到临时目录 →
# 以生产目录为工作区 read-tree 目标 tag 并比对 → 运行相关文件有差异就中止
# （此时生产目录还一个字节没动）→ 未入库的运行时产物打包、列出来 → 把 .git 移进来、
# HEAD 指向 tag → 落地文档类差异 → uv sync --frozen → 离线测试 → 记 state/deploy.history。
#
# 约束：macOS 自带 bash 3.2 与 BSD 工具。测试用的环境变量：MEDIA_AGENT_HOME UV_BIN
# LAUNCHCTL MA_LAUNCHD_LABEL DEPLOY_TEST_CMD
set -u
set -o pipefail
# macOS 的 tar 默认给带扩展属性的文件多打一个 ._文件（AppleDouble）；
# 打包带回开发机的东西解开后会多出一堆 ._ 垃圾文件。
export COPYFILE_DISABLE=1

APP=${MEDIA_AGENT_HOME:-$HOME/media-agent}
UV=${UV_BIN:-$HOME/.local/bin/uv}
REPO_URL=https://github.com/wzh4464/self-evolving-media-agent.git
LABEL=${MA_LAUNCHD_LABEL:-com.zihan.media-agent}
LAUNCHCTL=${LAUNCHCTL:-/bin/launchctl}

# 允许与 tag 不同的路径（前缀匹配）。生产上从没同步过这些文档 / 脚本 / 测试，
# 或者是旧版本（AGENTS.md 停在 1abcf1a）；转换时由 tag 的版本落地，不影响运行。
# 不在这里的一切——media_agent/、pyproject.toml、uv.lock、.agents/rules/、
# .agents/preferences.json——必须与 tag 一字不差，否则中止。
ALLOW="AGENTS.md README.md README.zh.md CHANGELOG.md LICENSE .gitignore .env.example
.github/ deploy/ tools/ tests/ .agents/notes/"

TAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repo)  shift; REPO_URL=${1:-} ;;
        --app)   shift; APP=${1:-} ;;
        --allow) shift; ALLOW="$ALLOW ${1:-}" ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) printf '✗ 未知参数：%s\n' "$1" >&2; exit 2 ;;
        *)  TAG=$1 ;;
    esac
    shift
done

MOVED=0
say()  { printf '%s\n' "$*"; }
die()  {
    printf '✗ %s\n' "$*" >&2
    [ "$MOVED" = 1 ] && rollback_hint >&2
    exit 1
}

[ -n "$TAG" ] || { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' \
    || die "只接受 tag（形如 v0.1.0），拒绝：$TAG"
[ -n "$REPO_URL" ] || die "--repo 需要参数"
[ -d "$APP" ] || die "找不到生产目录 $APP"
APP=$(cd "$APP" && pwd -P)
[ -e "$APP/.git" ] && die "$APP 已经是 git 工作区了——以后用 deploy/deploy.sh <tag>"
[ -d "$APP/media_agent" ] && [ -f "$APP/pyproject.toml" ] || die "$APP 不像 media-agent 目录"
[ -x "$UV" ] || UV=$(command -v uv 2>/dev/null || true)
[ -n "$UV" ] && [ -x "$UV" ] || die "找不到 uv（UV_BIN 或 ~/.local/bin/uv）"
[ -f "$APP/.env" ] || say "⚠️  $APP/.env 不存在（转换本身不需要它，但生产离不开它）"

if "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q 'state = running'; then
    die "launchd 的一轮正在跑（${LABEL}），等它结束再转换"
fi

TS=$(date '+%Y%m%dT%H%M%S')
PARENT=$(dirname "$APP")
BASE=$(basename "$APP")
SNAP="$PARENT/$BASE-pre-git-$TS.tgz"
HARVEST_DIR="$APP/state/harvest"
mkdir -p "$APP/state" "$HARVEST_DIR" || die "建不了 $APP/state"

CLONE=""
cleanup() { [ -n "$CLONE" ] && rm -rf "$CLONE"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# ------------------------------------------------------------------ 快照
# 含 .env（所以 600），不含 state/（14G 隔离区、转换不碰它）与 .venv（可重建）。
say "▶ 快照 → $SNAP"
( umask 077 && tar -czf "$SNAP" -C "$PARENT" \
    --exclude "$BASE/state" --exclude "$BASE/.venv" "$BASE" ) || die "打快照失败"
chmod 600 "$SNAP"

# ------------------------------------------------------------------ 克隆并比对
CLONE=$(mktemp -d "$PARENT/.$BASE-clone.XXXXXX") || die "mktemp -d 失败"
say "▶ git clone --no-checkout $REPO_URL"
git clone -q --no-checkout "$REPO_URL" "$CLONE/repo" || die "clone 失败"
GD="$CLONE/repo/.git"
G() { git --git-dir="$GD" --work-tree="$APP" "$@"; }

SHA=$(G rev-parse -q --verify "refs/tags/$TAG^{commit}") || die "仓库里没有 tag $TAG"
say "   $TAG = $SHA"

# 本机专用忽略规则：旧 tag 的 .gitignore 挡不住 .env.bak-* / *.bak-* / .DS_Store，
# 不补上它们会被当成"运行时产物"打进要带回开发机的包——里面有真密钥。
mkdir -p "$GD/info"
cat >> "$GD/info/exclude" <<'EXCLUDES'
.env
.env.*
!.env.example
state/
.venv/
__pycache__/
*.pyc
*.bak
*.bak-*
*.bak.*
.DS_Store
.pytest_cache/
.ruff_cache/
EXCLUDES

G read-tree "$SHA" || die "read-tree 失败"
G update-index -q --refresh >/dev/null 2>&1 || true

DIFF="$CLONE/diff.txt"
G -c core.quotePath=false diff --name-status > "$DIFF" || die "git diff 失败"

allowed() {
    local p=$1 a
    for a in $ALLOW; do
        case "$a" in
            */) case "$p" in "$a"*) return 0 ;; esac ;;
            *)  [ "$p" = "$a" ] && return 0 ;;
        esac
    done
    return 1
}

BLOCKED="$CLONE/blocked.txt"
ALLOWED_DIFF="$CLONE/allowed.txt"
: > "$BLOCKED"
: > "$ALLOWED_DIFF"
while IFS="$(printf '\t')" read -r st path; do
    [ -n "$path" ] || continue
    if allowed "$path"; then
        printf '%s\t%s\n' "$st" "$path" >> "$ALLOWED_DIFF"
    else
        printf '%s\t%s\n' "$st" "$path" >> "$BLOCKED"
    fi
done < "$DIFF"

if [ -s "$BLOCKED" ]; then
    say ""
    say "✗ 以下与运行相关的文件和 $TAG 不一致，中止（生产目录没有任何改动）："
    sed 's/^/    /' "$BLOCKED"
    say ""
    cut -f2 "$BLOCKED" | while IFS= read -r p; do
        G --no-pager diff -- "$p" | head -n 60 | sed 's/^/    /'
    done
    say ""
    say "  生产上跑的不是 ${TAG}。先查清楚差异从哪来，或者换一个与生产一致的 tag。"
    rm -f "$SNAP"
    exit 1
fi

# ------------------------------------------------------------------ 未入库的运行时产物
UNTRACKED="$CLONE/untracked.txt"
G -c core.quotePath=false ls-files --others --exclude-standard > "$UNTRACKED" \
    || die "git ls-files 失败"
# 双保险：规则怎么写都不许把这些打进包里
grep -vE '(^|/)\.env($|\.)|\.bak($|[-.])|^state/|^\.venv/' "$UNTRACKED" \
    > "$UNTRACKED.safe" || true
N_UNTRACKED=$(grep -c . "$UNTRACKED.safe" || true)
HARVEST=""
if [ "$N_UNTRACKED" -gt 0 ]; then
    HARVEST="$HARVEST_DIR/convert-$TS.tgz"
    tar -czf "$HARVEST" -C "$APP" -T "$UNTRACKED.safe" || die "打包运行时产物失败"
    # 标出上游默认分支里已经有同样内容的（比如 bb51f98 入库的 28 条演进规则）：
    # 它们不用再提交，部署含它们的 tag 时 deploy.sh 会认出来并交给 tag 接管。
    N_NEW=0
    while IFS= read -r f; do
        want=$(G rev-parse -q --verify "refs/remotes/origin/HEAD:$f" 2>/dev/null || true)
        have=$(git --git-dir="$GD" hash-object -- "$APP/$f" 2>/dev/null || true)
        if [ -n "$want" ] && [ "$want" = "$have" ]; then
            printf '%s\t（上游默认分支已有，内容相同）\n' "$f"
        else
            printf '%s\t（git 里没有——需要提交）\n' "$f"
            N_NEW=$((N_NEW + 1))
        fi
    done < "$UNTRACKED.safe" > "$UNTRACKED.notes"
fi

# ------------------------------------------------------------------ 就位
CREATED="$HARVEST_DIR/convert-$TS.created.txt"
awk -F '\t' '$1 == "D" { print $2 }' "$ALLOWED_DIFF" > "$CREATED"
rollback_hint() {
    say "  撤销转换（运行代码从头到尾没变过）："
    say "    rm -rf $APP/.git"
    say "    tar -xzf $SNAP -C $PARENT"
    say "    (cd $APP && xargs rm -f < $CREATED)   # 删掉转换时新落地的文件"
}

say "▶ 把 .git 移进 ${APP}，HEAD 指向 $TAG"
mv "$GD" "$APP/.git" || die "移动 .git 失败"
MOVED=1
g() { git -C "$APP" "$@"; }
g update-ref --no-deref HEAD "$SHA" || die "update-ref 失败"

# 只落地允许有差异的那些（文档、脚本、测试）；运行相关文件已证明一字不差
if [ -s "$ALLOWED_DIFF" ]; then
    g -c advice.detachedHead=false checkout -q -- . || die "checkout 失败"
fi
LEFT=$(g status --porcelain --untracked-files=no)
[ -z "$LEFT" ] || die "落地后仍有差异：$LEFT"

say "▶ uv sync --frozen"
(cd "$APP" && env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT "$UV" sync --frozen) \
    || die "uv sync --frozen 失败（回退办法见下）"

say "▶ 离线测试"
(
    cd "$APP" || exit 1
    export PYTHONDONTWRITEBYTECODE=1 MEDIA_AGENT_DEPLOYING=1
    unset MEDIA_AGENT_LIVE
    if [ -n "${DEPLOY_TEST_CMD:-}" ]; then
        /bin/sh -c "$DEPLOY_TEST_CMD"
    elif [ -f tests/conftest.py ] && .venv/bin/python -c 'import pytest' 2>/dev/null; then
        .venv/bin/python -m pytest -q -p no:cacheprovider
    else
        # v0.1.0：脚本式测试；test_no_phantom_duplicate 连生产库，不跑
        for t in tests/test_grab_bookkeeping.py tests/test_seal_slot.py; do
            [ -f "$t" ] || continue
            .venv/bin/python "$t" || exit 1
        done
    fi
)
TEST_RC=$?

printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$TAG" "$SHA" "pre-git" \
    "$([ "$TEST_RC" = 0 ] && echo converted || echo converted-tests-failed)" \
    "snapshot=$SNAP" >> "$APP/state/deploy.history"

say ""
say "═══ 转换完成：$APP 现在是 ${TAG}（$(g rev-parse --short HEAD)）的 git 工作区 ═══"
if [ -s "$ALLOWED_DIFF" ]; then
    say "  由 tag 落地的文档 / 脚本（生产上原先缺失或是旧版）："
    sed 's/^/    /' "$ALLOWED_DIFF"
fi
if [ -n "$HARVEST" ]; then
    say ""
    say "  $TAG 不跟踪、生产上却有的文件 $N_UNTRACKED 个（其中 $N_NEW 个 git 里没有），"
    say "  已打包到 ${HARVEST}："
    sed 's/^/    /' "$UNTRACKED.notes"
    say "  它们原样留在工作区。git 里没有的那些带回开发机审阅、提交，打新 tag 之后"
    say "  deploy.sh 才会放行（漂移闸门）："
    say "    scp <生产机>:$HARVEST . && tar -xzf $(basename "$HARVEST") -C <仓库>"
fi
say ""
rollback_hint
if [ "$TEST_RC" != 0 ]; then
    say ""
    say "✗ 离线测试失败（代码与转换前一致，多半是环境问题）。先看上面的输出；需要时按上面撤销。"
    exit 1
fi
