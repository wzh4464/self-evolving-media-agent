#!/bin/bash
# deploy.sh —— 把生产目录（默认 ~/media-agent）原地切到一个 git tag。只接受 tag。
#
#   deploy/deploy.sh v0.2.0             部署
#   deploy/deploy.sh v0.2.0 --check     只跑漂移闸门与暂存验证，不切换
#   deploy/deploy.sh v0.2.0 --harvest   漂移时顺手把漂移文件打包到 state/harvest/
#   deploy/deploy.sh v0.2.0 --bundle F  GitHub 不通时从 git bundle 取 tag
#   回滚 = deploy.sh <上一个 tag>（见 state/deploy.history）
#
# 每一步的理由见 deploy/README.md。约束：生产机是 macOS 自带的 bash 3.2 与 BSD 工具
# （没有 mapfile / 关联数组 / GNU 专有参数 / timeout），uv 0.7.2，git 2.50。
#
# 为什么原地切换、不用 releases/<sha> + current 软链：config.py 等三处用
# Path(__file__).resolve() 定位项目根，软链会被解析成各个发布目录——state/、.env、
# 规则、偏好都会跟着"分家"；隔离区审计里存的又是绝对路径（deploy 调研 §5）。
#
# 给测试用的环境变量：MEDIA_AGENT_HOME UV_BIN DEPLOY_REMOTE DEPLOY_LOCK_WAIT
# LAUNCHCTL LAUNCH_AGENTS_DIR MA_LAUNCHD_LABEL DEPLOY_TEST_CMD LAUNCHD_BOOTSTRAP_TRIES
set -u
set -o pipefail
# ssh 断线（SIGHUP）不许把部署腰斩在半路。以前只有切换阶段的 `trap … EXIT`：断线打在
# 原地 `uv sync` / 离线测试上，HEAD 已是新 tag、venv 还是旧锁文件装的、plist 没换、
# deploy.history 一行没有（2026-09-26 审查复现，rc=129）。忽略 HUP 会被 lockf、git、uv、
# pytest 一路继承——一旦开始，要么完整做完，要么完整退回。Ctrl-C / SIGTERM 仍然有效：
# 主阶段直接退出（此时生产目录还没动），切换阶段则自动退回（见 switch_phase）。
trap '' HUP
# macOS 的 tar 默认给带扩展属性的文件多打一个 ._文件（AppleDouble）；
# 打包带回开发机的东西解开后会多出一堆 ._ 垃圾文件。
export COPYFILE_DISABLE=1

APP=${MEDIA_AGENT_HOME:-$HOME/media-agent}
UV=${UV_BIN:-$HOME/.local/bin/uv}
REMOTE=${DEPLOY_REMOTE:-origin}
LOCK_WAIT=${DEPLOY_LOCK_WAIT:-900}
LABEL=${MA_LAUNCHD_LABEL:-com.zihan.media-agent}
LAUNCHCTL=${LAUNCHCTL:-/bin/launchctl}
AGENTS_DIR=${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}
PLIST_NAME="$LABEL.plist"
BOOTSTRAP_TRIES=${LAUNCHD_BOOTSTRAP_TRIES:-5}

TAG=""
MODE=deploy          # deploy | check
HARVEST=0
FETCH=1
BUNDLE=""
PHASE=main           # main | switch（内部：持运行锁后的切换阶段）

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

say()  { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --check)    MODE=check ;;
        --harvest)  HARVEST=1 ;;
        --no-fetch) FETCH=0 ;;
        --bundle)   shift; BUNDLE=${1:-}; [ -n "$BUNDLE" ] || die "--bundle 需要文件参数" ;;
        --app)      shift; APP=${1:-}; [ -n "$APP" ] || die "--app 需要目录参数" ;;
        --__switch) PHASE=switch ;;
        -h|--help)  usage; exit 0 ;;
        -*)         die "未知参数：$1（--help 看用法）" ;;
        *)          [ -z "$TAG" ] || die "只能给一个 tag"; TAG=$1 ;;
    esac
    shift
done

[ -n "$TAG" ] || { usage; exit 2; }
# 只接受发布 tag 的形状。分支名、commit、HEAD~1 一律拒绝：生产上跑的必须是一个
# 有名字、不可移动、能在 CHANGELOG 里找到的版本（CHANGELOG「版本与发布约定」）。
printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' \
    || die "只部署 tag（形如 v0.2.0），拒绝：$TAG"

[ -d "$APP" ] || die "找不到生产目录 $APP"
APP=$(cd "$APP" && pwd -P)
[ -e "$APP/.git" ] || die "$APP 还不是 git 工作区——先跑一次 deploy/convert-to-git.sh"
[ -x "$UV" ] || UV=$(command -v uv 2>/dev/null || true)
[ -n "$UV" ] && [ -x "$UV" ] || die "找不到 uv（UV_BIN 或 ~/.local/bin/uv）"
mkdir -p "$APP/state" || die "建不了 $APP/state"
LOCK="$APP/state/run.lock"
HISTORY="$APP/state/deploy.history"

g() { git -C "$APP" "$@"; }

now_iso() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# state/deploy.history：一次部署尝试一行，制表符分隔
#   时间  tag  目标 commit  部署前 commit  结果  说明
record() {
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(now_iso)" "$TAG" "${SHA:--}" "${PREV:--}" \
        "$1" "${2:-}" >> "$HISTORY"
}

# with_lock <锁文件> <最多等几秒> <命令...>
# 拿不到锁返回 75（EX_TEMPFAIL）。与 media_agent/runlock.py 是同一种锁（flock），
# 锁文件一律保留：删掉它会让下一个按路径打开的人拿到另一把锁。
#
# **持锁的一方必须等命令收尾再退出。** 切换阶段被 Ctrl-C / SIGTERM 打断时要"自动退回"，
# 而信号是打给整个进程组的：持锁的包装进程若先死，退回就成了后台孤儿，外层脚本已经
# 返回，看起来像"中断了却没退回"。macOS 的 /usr/bin/lockf 在等子进程时忽略 SIGINT，
# 天然没事；util-linux 的 `flock 文件 命令` 不处理任何信号，收到就死——2026-09-26 v0.2.0
# 的 CI 在 Ubuntu 上正是这样挂的（本机 macOS 走 lockf，测不到）。所以 flock 分支改成
# 在子 shell 里用描述符持锁、命令做前台子进程（bash 等前台子进程结束才处理信号），
# Python 兜底同样忽略 INT/TERM、等子进程退出。DEPLOY_LOCK_IMPL 只供测试强制走某一分支。
with_lock() {
    local file=$1 wait=$2 impl=${DEPLOY_LOCK_IMPL:-auto}
    shift 2
    if [ "$impl" = auto ]; then
        if [ -x /usr/bin/lockf ]; then impl=lockf
        elif command -v flock >/dev/null 2>&1; then impl=flock
        else impl=python
        fi
    fi
    case $impl in
    lockf)
        /usr/bin/lockf -k -s -t "$wait" "$file" "$@"
        ;;
    flock)
        (
            # >> 而不是 >：锁文件里有持有者的自述，打开时不能截断
            exec 9>>"$file" || exit 75
            flock -w "$wait" 9 || exit 75
            "$@"
        )
        ;;
    python)
        python3 -c '
import fcntl, os, signal, subprocess, sys, time
path, wait, cmd = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
end = time.time() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.time() >= end:
            sys.exit(75)
        time.sleep(0.5)
# 同进程组的子进程自己会收到打给进程组的信号；这里只负责持锁并等它收尾
for s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(s, signal.SIG_IGN)
p = subprocess.Popen(cmd, preexec_fn=lambda: [signal.signal(s, signal.SIG_DFL)
                                              for s in (signal.SIGINT, signal.SIGTERM)])
sys.exit(p.wait())
' "$file" "$wait" "$@"
        ;;
    *)
        echo "DEPLOY_LOCK_IMPL 只能是 auto / lockf / flock / python，收到 $impl" >&2
        return 2
        ;;
    esac
}

uv_in() {   # uv_in <目录> <uv 参数...>：不让外面的虚拟环境变量把 uv 引到别的 venv
    local dir=$1
    shift
    (cd "$dir" && env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT "$UV" "$@")
}

# 离线测试。DEPLOY_TEST_CMD 只给部署脚本自己的测试用（换成一条轻量命令）。
# MEDIA_AGENT_DEPLOYING=1 让测试集里"测部署脚本"的那些用例跳过自己，避免递归。
run_tests() {
    (
        cd "$1" || exit 1
        export PYTHONDONTWRITEBYTECODE=1 MEDIA_AGENT_DEPLOYING=1
        unset MEDIA_AGENT_LIVE
        if [ -n "${DEPLOY_TEST_CMD:-}" ]; then
            /bin/sh -c "$DEPLOY_TEST_CMD"
        elif [ -f tests/conftest.py ]; then
            .venv/bin/python -m pytest -q -p no:cacheprovider
        else
            # v0.1.0 及更早只有脚本式测试；test_no_phantom_duplicate 连生产库，不跑
            for t in tests/test_grab_bookkeeping.py tests/test_seal_slot.py; do
                [ -f "$t" ] || continue
                .venv/bin/python "$t" || exit 1
            done
        fi
    )
}

# 本机专用的忽略规则（.git/info/exclude，不入库）。旧 tag 的 .gitignore 挡不住
# .env.bak-*、*.bak-*、.DS_Store——它们会被漂移闸门当成"未入库的文件"，
# 更糟的是会被 --harvest 打进要带回开发机的包里。
LOCAL_EXCLUDES='.env
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
.ruff_cache/'

ensure_local_excludes() {
    local f line
    f=$(g rev-parse --git-path info/exclude) || return 1
    case "$f" in /*) ;; *) f="$APP/$f" ;; esac
    mkdir -p "$(dirname "$f")"
    touch "$f"
    printf '%s\n' "$LOCAL_EXCLUDES" | while IFS= read -r line; do
        grep -qxF -- "$line" "$f" || printf '%s\n' "$line" >> "$f"
    done
}

is_secret() {   # 无论规则怎么写，这些路径永远不进 harvest 包
    case "$1" in
        .env.example) return 1 ;;
        .env|.env.*|*/.env|*/.env.*|*.bak|*.bak-*|*.bak.*|state/*|.venv/*) return 0 ;;
    esac
    return 1
}

harvest() {   # harvest <清单文件> <标签>
    local list=$1 label=$2 out safe
    safe=$(mktemp "${TMPDIR:-/tmp}/ma-harvest.XXXXXX")
    while IFS= read -r f; do
        is_secret "$f" || printf '%s\n' "$f"
    done < "$list" > "$safe"
    if [ ! -s "$safe" ]; then rm -f "$safe"; return 0; fi
    mkdir -p "$APP/state/harvest"
    out="$APP/state/harvest/$TS-$label.tgz"
    if tar -czf "$out" -C "$APP" -T "$safe"; then
        say "📦 已打包到 ${out}："
        sed 's/^/     /' "$safe"
        say "   带回开发机：scp <生产机>:$out . && tar -xzf $(basename "$out") -C <仓库>"
        say "   审阅后提交、打新 tag，再部署那个 tag。"
    else
        warn "打包失败：$out"
    fi
    rm -f "$safe"
}

launchd_running() {
    "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q 'state = running'
}

# 转换前的老代码（v0.1.0）不认运行锁：它的一轮可能正在跑，锁挡不住它。
wait_launchd_idle() {
    local waited=0
    while launchd_running; do
        [ "$waited" -ge "$LOCK_WAIT" ] && return 1
        [ "$waited" -eq 0 ] && say "   launchd 的一轮正在跑，等它结束…"
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

PLIST_CHANGED=0

launchd_load() {   # launchd_load <plist>：bootout 旧的（没装过也无妨）再 bootstrap
    local domain n=0
    domain="gui/$(id -u)"
    "$LAUNCHCTL" bootout "$domain/$LABEL" >/dev/null 2>&1 || true
    # bootout 是异步收尾的，紧接着 bootstrap 偶尔报 "Input/output error"，重试几次
    until "$LAUNCHCTL" bootstrap "$domain" "$1"; do
        n=$((n + 1))
        [ "$n" -ge "$BOOTSTRAP_TRIES" ] && return 1
        sleep 1
    done
}

install_plist() {
    local src="$APP/deploy/$PLIST_NAME" dst="$AGENTS_DIR/$PLIST_NAME"
    if [ ! -f "$src" ]; then
        say "   tag 里没有 deploy/${PLIST_NAME}，launchd 不动"
        return 0
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        say "   launchd 配置未变"
        return 0
    fi
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint -s "$src" || { warn "plutil 校验失败：$src"; return 1; }
    fi
    PLIST_CHANGED=1
    mkdir -p "$AGENTS_DIR"
    cp "$src" "$dst.new" && mv "$dst.new" "$dst" || return 1
    launchd_load "$dst" || { warn "launchctl bootstrap 失败"; return 1; }
    say "   launchd 配置已更新并重新加载（StartInterval 从现在重新计时）"
}

restore_plist() {
    local dst="$AGENTS_DIR/$PLIST_NAME"
    [ "$PLIST_CHANGED" = 1 ] || return 0
    if [ -f "$BK/$PLIST_NAME" ]; then
        cp "$BK/$PLIST_NAME" "$dst" && launchd_load "$dst"
    else
        "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
        rm -f "$dst"
    fi
}

# ===================================================================== 切换阶段
# 由主阶段在运行锁里调起（with_lock … --__switch）。一切信息经环境变量传进来。
switch_phase() {
    SHA=$_MA_SHA
    PREV=$_MA_PREV
    PREV_DESC=$_MA_PREV_DESC
    TS=$_MA_TS
    REPLACE_LIST=$_MA_REPLACE_LIST
    BK="$APP/state/backups/$TS-$PREV_DESC"

    # 锁文件里写一句自述：launchd 起来的 run 拿不到锁时会把它打印出来。
    # 退出前清掉（锁文件本身保留），免得以后有人拿到一句过期的说明。
    printf 'pid=%s cmd=deploy.sh %s since=%s\n' "$$" "$TAG" \
        "$(date '+%Y-%m-%dT%H:%M:%S')" > "$LOCK"
    trap ': > "$LOCK"' EXIT

    if ! wait_launchd_idle; then
        record busy "launchd 的一轮 $LOCK_WAIT 秒内没结束"
        die "launchd 的一轮 $LOCK_WAIT 秒内没结束，没有切换"
    fi

    # 小体量状态留一份（审计 / 清理记录 / 缓存 / 已装的 plist）。代码回滚从不回卷
    # audit.jsonl——数据回退仍是 media-agent rollback；这份只供手工比对。
    mkdir -p "$BK" || die "建不了备份目录 $BK"
    local f
    for f in audit.jsonl purge.jsonl cache.sqlite3 deploy.history; do
        [ -f "$APP/state/$f" ] && cp -p "$APP/state/$f" "$BK/"
    done
    [ -f "$AGENTS_DIR/$PLIST_NAME" ] && cp -p "$AGENTS_DIR/$PLIST_NAME" "$BK/"

    # 从这里开始动生产目录。Ctrl-C / SIGTERM 一律自动退回（HUP 在脚本开头就忽略了）：
    # 被打断的切换不能停在"新代码 + 旧 venv + 旧 plist"上。
    trap 'revert "被信号中断（INT/TERM）"' INT TERM

    # 与目标 tag 内容完全相同的未入库文件：先挪开，否则 checkout 拒绝覆盖。
    # 切换失败要退回去时从这个包里放回来（旧 tag 不跟踪它们，checkout 会把它们删掉）。
    if [ -s "$REPLACE_LIST" ]; then
        tar -czf "$BK/replaced-untracked.tgz" -C "$APP" -T "$REPLACE_LIST" \
            || die "打包待替换文件失败，没有切换"
        while IFS= read -r f; do rm -f "$APP/$f"; done < "$REPLACE_LIST"
    fi

    say "▶ 切换到 ${TAG}（${SHA}）"
    if ! g -c advice.detachedHead=false checkout -q --detach "$SHA"; then
        revert "git checkout"
    fi
    say "   uv sync --frozen"
    uv_in "$APP" sync --frozen || revert "uv sync --frozen"
    say "   离线测试（原地）"
    run_tests "$APP" || revert "原地离线测试"
    install_plist || revert "安装 launchd 配置"

    trap - INT TERM                   # 已经完整切换，此后的中断不该再退回
    record ok "from $PREV_DESC"
    local ver
    if ver=$("$APP/.venv/bin/media-agent" --version 2>/dev/null); then
        say "✅ 已部署 ${TAG}（$(g rev-parse --short HEAD)）：$ver"
        case "$ver" in
            *" ${TAG#v}") ;;
            *) warn "pyproject 里的版本号（${ver##* }）与 tag $TAG 不一致——发版时忘了改版本号？" ;;
        esac
    else
        say "✅ 已部署 ${TAG}（$(g rev-parse --short HEAD)）（这个版本还没有 media-agent --version）"
    fi
    rollback_hint
    exit 0
}

# 部署成功后打印"怎么回到部署前的版本"。刚部署的 tag 里可能**没有** deploy/deploy.sh
# （v0.1.0 及更早）：那时照着"deploy/deploy.sh v0.2.0"做只会得到 No such file or
# directory——偏偏是在恢复的时候。所以从带脚本的 tag 里取一份到临时目录再跑。
rollback_hint() {
    local src tmp
    if [ "$PREV" = "$SHA" ]; then
        say "   （在同一版本上补齐了部署；回滚到更早的版本见 state/deploy.history）"
        return 0
    fi
    if ! printf '%s' "$PREV_DESC" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$' \
        || ! g rev-parse -q --verify "refs/tags/$PREV_DESC" >/dev/null; then
        say "   部署前的版本不是 tag（${PREV_DESC}），回滚只能手工 checkout $PREV"
        return 0
    fi
    if g cat-file -e "HEAD:deploy/deploy.sh" 2>/dev/null; then
        say "   回滚：deploy/deploy.sh $PREV_DESC"
        return 0
    fi
    src=$PREV_DESC
    g cat-file -e "$src:deploy/deploy.sh" 2>/dev/null || src="<任一带 deploy/deploy.sh 的 tag>"
    tmp="${TMPDIR:-/tmp}"
    tmp="${tmp%/}/media-agent-deploy.sh"
    say "   回滚（${TAG} 里没有 deploy/deploy.sh，从 ${src} 里取一份来跑）："
    say "     git -C $APP show ${src}:deploy/deploy.sh > $tmp && /bin/bash $tmp ${PREV_DESC}"
}

revert() {
    local why=$1 ok=1
    trap '' INT TERM                  # 退回本身不能再被打断
    warn "$why 失败，退回 ${PREV_DESC}（${PREV}）"
    g -c advice.detachedHead=false checkout -q -f --detach "$PREV" || ok=0
    if [ -f "$BK/replaced-untracked.tgz" ]; then
        tar -xzf "$BK/replaced-untracked.tgz" -C "$APP" || ok=0
    fi
    uv_in "$APP" sync --frozen || ok=0
    restore_plist || ok=0
    if [ "$ok" = 1 ]; then
        record reverted "$why"
        die "部署 $TAG 失败（${why}），已退回 $PREV_DESC"
    fi
    record revert-failed "$why"
    printf '\n✗✗ 部署 %s 失败（%s），而且没能完整退回 %s。\n' "$TAG" "$why" "$PREV_DESC" >&2
    printf '   手工处理：cd %s && git checkout -f --detach %s && %s sync --frozen\n' \
        "$APP" "$PREV" "$UV" >&2
    printf '   被挪开的未入库文件在 %s/replaced-untracked.tgz\n' "$BK" >&2
    exit 2
}

if [ "$PHASE" = switch ]; then
    switch_phase
fi

# ===================================================================== 主阶段
# 先把自己拷一份再执行：切换会原地改写 deploy/deploy.sh，bash 是边读边执行的。
# 同时拿 state/deploy.lock，两个部署不会交错。
if [ -z "${_MA_DEPLOY_COPY:-}" ]; then
    # 全部输出同时追加到 state/deploy.log。ssh 断线之后终端没了，子进程再往终端写会
    # 拿到 EIO（uv / pytest 可能因此失败）；经 tee 转一道，子进程写的是管道，tee 写终端
    # 失败只报一句、照样把日志写完。重新连上来看 state/deploy.log 就知道这次部署的结局。
    printf '\n===== %s deploy.sh %s =====\n' "$(now_iso)" "${ORIG_ARGS[*]}" \
        >> "$APP/state/deploy.log" 2>/dev/null || true
    # tee 必须挺过 INT/TERM：打给进程组的信号若先杀了它，后面"自动退回"一往终端写就
    # 吃 SIGPIPE，退回死在半路。bash 只让异步进程自动忽略 INT，TERM 得自己挡。
    exec > >(trap '' INT TERM; exec tee -a "$APP/state/deploy.log") 2>&1
    copy=$(mktemp "${TMPDIR:-/tmp}/media-agent-deploy.XXXXXX") || die "mktemp 失败"
    cp "$0" "$copy" || die "复制部署脚本失败"
    export _MA_DEPLOY_COPY=$copy
    # 这一层也要等副本收尾：没有 trap 的 SIGTERM 会让非交互 bash 立刻死掉（SIGINT 才会
    # 等前台子进程），调用方于是在"退回"还没做完时就拿到了返回码。设了 trap，bash 就会
    # 等前台命令结束再处理信号，返回码照实是副本的返回码。
    trap ':' INT TERM
    with_lock "$APP/state/deploy.lock" 0 "${BASH:-/bin/bash}" "$copy" "${ORIG_ARGS[@]}"
    rc=$?
    rm -f "$copy"
    [ "$rc" = 75 ] && die "另一个 deploy.sh 正在进行（$APP/state/deploy.lock）"
    exit "$rc"
fi

TS=$(date '+%Y%m%dT%H%M%S')
SHA=""
PREV=""
STAGE_PARENT=""
STAGE=""
TMPFILES=""

# shellcheck disable=SC2329  # 由 trap 调用
cleanup() {
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
        g worktree remove --force "$STAGE" >/dev/null 2>&1 || true
    fi
    [ -n "$STAGE_PARENT" ] && rm -rf "$STAGE_PARENT"
    g worktree prune >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    [ -n "$TMPFILES" ] && rm -f $TMPFILES
}
trap cleanup EXIT
trap 'exit 130' INT TERM

[ -f "$APP/.env" ] || die "$APP/.env 不存在——生产配置丢了？没有它部署出去也跑不起来"

# ------------------------------------------------------------------ 取 tag
if [ -n "$BUNDLE" ]; then
    say "▶ 从 bundle 取 tag：$BUNDLE"
    g bundle verify -q "$BUNDLE" >/dev/null 2>&1 || die "bundle 校验失败：$BUNDLE"
    g fetch -q "$BUNDLE" 'refs/tags/*:refs/tags/*' || die "从 bundle 取 tag 失败"
elif [ "$FETCH" = 1 ]; then
    say "▶ git fetch --tags $REMOTE"
    # tag 被人在上游挪过的话 git 会拒绝覆盖本地的同名 tag（would clobber）——
    # 这正是我们要的：已部署过的版本名不许换内容。
    g fetch -q --tags "$REMOTE" || die "fetch 失败（GitHub 不通可以用 --bundle，见 deploy/README.md）"
fi

SHA=$(g rev-parse -q --verify "refs/tags/$TAG^{commit}") || { SHA=""; die "没有这个 tag：$TAG"; }
PREV=$(g rev-parse -q --verify HEAD) || die "读不到当前 HEAD"
PREV_DESC=$(g describe --tags --always HEAD 2>/dev/null || printf '%s' "${PREV:0:12}")
PREV_DESC=$(printf '%s' "$PREV_DESC" | tr '/' '_')

say "   当前：${PREV_DESC}（${PREV}）"
say "   目标：${TAG}（${SHA}）"

# 最近一次**动过生产目录**的部署记录（drift / stage-failed / checked / lock-timeout /
# busy 都没动过，不算）：打印 "<目标 commit> <结果>"。
last_effective() {
    [ -f "$HISTORY" ] || return 0
    awk -F'\t' '$5 ~ /^(ok|converted|converted-tests-failed|reverted|revert-failed)$/ \
        { r = $3 " " $5 } END { if (r != "") print r }' "$HISTORY"
}

# HEAD 已经指向目标 tag 不等于部署成功过：切换阶段被杀掉（旧版本没有信号处理）时，
# HEAD 停在新 tag、venv 与 plist 还是旧的。只有 deploy.history 最近一次动过生产目录的
# 记录就是这个 commit 的成功部署（或转换）时才短路；否则照常走一遍，把依赖、测试、
# launchd 配置重新落实——对已经完好的目录，这一遍是幂等的。
if [ "$SHA" = "$PREV" ] && [ "$MODE" = deploy ]; then
    case "$(last_effective)" in
        "$SHA ok"|"$SHA converted")
            say "✅ 已经是 ${TAG}，无需部署"
            exit 0 ;;
    esac
    say "   HEAD 已是 ${TAG}，但 deploy.history 里没有它部署成功的记录（上次部署可能中途被打断）："
    say "   照常走一遍，把依赖、测试、launchd 配置重新落实"
fi

# ------------------------------------------------------------------ 漂移闸门
# 生产目录必须与当前部署的 tag 一字不差：改过的被跟踪文件（比如在生产上手改了
# .agents/preferences.json）、多出来的未入库文件（比如演进器写的规则）都说明
# "生产行为已经不能从 git 复现"。切换要么把它们覆盖掉、要么被它们挡住——
# 两样都不能静悄悄地发生。要保留就提交入库、打 tag 再部署。
say "▶ 漂移闸门"
ensure_local_excludes || die "写不了 .git/info/exclude"
g update-index -q --refresh >/dev/null 2>&1 || true

MODIFIED=$(mktemp "${TMPDIR:-/tmp}/ma-mod.XXXXXX")
UNTRACKED=$(mktemp "${TMPDIR:-/tmp}/ma-untracked.XXXXXX")
DRIFT=$(mktemp "${TMPDIR:-/tmp}/ma-drift.XXXXXX")
REPLACE_LIST=$(mktemp "${TMPDIR:-/tmp}/ma-replace.XXXXXX")
TMPFILES="$MODIFIED $UNTRACKED $DRIFT $REPLACE_LIST"

g -c core.quotePath=false status --porcelain --untracked-files=no > "$MODIFIED" \
    || die "git status 失败"
g -c core.quotePath=false ls-files --others --exclude-standard > "$UNTRACKED" \
    || die "git ls-files 失败"

N_DRIFT_UNTRACKED=0
while IFS= read -r f; do
    [ -n "$f" ] || continue
    want=$(g rev-parse -q --verify "$SHA:$f" 2>/dev/null || true)
    have=$(g hash-object -- "$f" 2>/dev/null || true)
    if [ -n "$want" ] && [ "$want" = "$have" ]; then
        printf '%s\n' "$f" >> "$REPLACE_LIST"      # tag 里有一模一样的：切换时由 tag 接管
    else
        if [ -n "$want" ]; then why="$TAG 里有同名文件但内容不同"; else why="$TAG 里没有"; fi
        printf '%s\t%s\n' "$f" "$why" >> "$DRIFT"
        N_DRIFT_UNTRACKED=$((N_DRIFT_UNTRACKED + 1))
    fi
done < "$UNTRACKED"

N_MODIFIED=$(grep -c . "$MODIFIED" || true)
if [ "$N_MODIFIED" -gt 0 ] || [ "$N_DRIFT_UNTRACKED" -gt 0 ]; then
    say ""
    say "✗ 生产目录与当前部署的 $PREV_DESC 不一致，拒绝部署。"
    if [ "$N_MODIFIED" -gt 0 ]; then
        say ""
        say "  被改动的已跟踪文件（$N_MODIFIED 个）："
        sed 's/^/    /' "$MODIFIED"
        say ""
        g --no-pager diff HEAD --stat | sed 's/^/    /'
        g --no-pager diff HEAD | head -n 200 | sed 's/^/    /'
    fi
    if [ "$N_DRIFT_UNTRACKED" -gt 0 ]; then
        say ""
        say "  未入库的文件（$N_DRIFT_UNTRACKED 个）："
        sed 's/^/    /' "$DRIFT"
    fi
    say ""
    say "  处理：要保留的改动带回开发机提交、打新 tag 再部署那个 tag；"
    say "        不要的改动：git -C $APP checkout -- <文件> / 移出工作区。"
    if [ "$HARVEST" = 1 ]; then
        HLIST=$(mktemp "${TMPDIR:-/tmp}/ma-hlist.XXXXXX")
        TMPFILES="$TMPFILES $HLIST"
        { cut -c4- "$MODIFIED"; cut -f1 "$DRIFT"; } | grep . > "$HLIST"
        harvest "$HLIST" drift
    else
        say "        加 --harvest 把它们打包到 state/harvest/ 方便带走。"
    fi
    record drift "$N_MODIFIED modified, $N_DRIFT_UNTRACKED untracked"
    exit 1
fi
N_REPLACE=$(grep -c . "$REPLACE_LIST" || true)
say "   干净（另有 $N_REPLACE 个未入库文件与 $TAG 里的内容完全相同，切换时由 tag 接管）"

# ------------------------------------------------------------------ 暂存验证
# 在独立的 git worktree + 独立 venv 里把目标 tag 装一遍、测一遍。生产目录此时
# 一个字节都没动；失败就到此为止。
STAGE_PARENT=$(mktemp -d "${TMPDIR:-/tmp}/media-agent-stage.XXXXXX") || die "mktemp -d 失败"
STAGE="$STAGE_PARENT/$TAG"
say "▶ 暂存验证：$STAGE"
g worktree add -q --detach "$STAGE" "$SHA" || die "git worktree add 失败"

stage_fail() {
    record stage-failed "$1"
    die "暂存验证失败（$1），生产目录没有任何改动"
}
say "   uv sync --frozen"
uv_in "$STAGE" sync --frozen || stage_fail "uv sync --frozen"
say "   uv lock --check"
uv_in "$STAGE" lock --check || stage_fail "uv lock --check：uv.lock 与 pyproject.toml 不一致"
say "   离线测试"
run_tests "$STAGE" || stage_fail "离线测试"
say "   通过"

if [ "$MODE" = check ]; then
    record checked "stage ok"
    say "✅ --check：$TAG 可以部署（没有切换）"
    exit 0
fi

# ------------------------------------------------------------------ 切换（持运行锁）
say "▶ 等运行锁 ${LOCK}（最多 $LOCK_WAIT 秒）"
export _MA_SHA=$SHA _MA_PREV=$PREV _MA_PREV_DESC=$PREV_DESC _MA_TS=$TS
export _MA_REPLACE_LIST=$REPLACE_LIST
with_lock "$LOCK" "$LOCK_WAIT" "${BASH:-/bin/bash}" "$_MA_DEPLOY_COPY" "$TAG" \
    --app "$APP" --__switch
rc=$?
if [ "$rc" = 75 ]; then
    holder=$(cat "$LOCK" 2>/dev/null || true)
    record lock-timeout "${holder:-持有者未知}"
    die "等了 $LOCK_WAIT 秒仍拿不到运行锁（${holder:-持有者未知}），没有切换"
fi
exit "$rc"
