# bash 5.3 的子 shell 执行父进程的 INT trap：部署被 Ctrl-C 打断时退回跑丢了

**日期**: 2026-11-17
**状态**: implemented / bug-fix
**触发**: PR #4 的 CI 两个 Ubuntu 腿红，`test_deploy_scripts` 里 INT 那几格（flock / python 两种持锁实现都红，
TERM 全绿）。对原样的 v0.5.1（df72b27）重跑同样红；9 月同一个 commit 是绿的。GitHub 的 `ubuntu-latest`
在此期间换成了 ubuntu-26.04（bash 5.3.9、uutils coreutils）。

## 怎么定位的

本机拉不下 Linux 镜像，在调试分支上用 CI 复现：

1. 排除 tee：uutils 的 `tee` 经 `exec` 后保留了继承来的 SIG_IGN（`/proc/<pid>/status` 的 SigIgn 有 INT、TERM）。
2. 进程链信号处置：切换阶段的 bash 确实装着 INT 的 handler。
3. 切换阶段开 xtrace：`revert` 是在 `run_tests` 的 `( … )` 子 shell 里执行的（`++` 层级），打印第一行
   警告后子 shell 就退出了；父进程之后没有任何动作，HEAD 停在新 tag，history 没有 `reverted`。

即 bash 5.3 的子 shell 在等前台命令时收到 SIGINT，会执行从父进程继承来的 trap 文本（3.2 / 5.2 下子 shell
把被捕获的信号复位成默认）。最小脚本复现不出来，和父子两层都在等前台进程、同时收到进程组信号的时序有关。

## 做法

切换窗口里两个子 shell（`uv_in`、`run_tests`）进去第一件事 `trap - INT TERM`：子 shell 按默认处置随前台命令
结束，退回只由切换阶段的 bash 执行。生产机是 macOS 的 bash 3.2，行为不变。

## 测试

现有的 `test_interrupt_during_the_in_place_switch_reverts[*-INT]` 与
`test_interrupt_reverts_even_when_pytest_was_started_with_sigint_ignored` 就是回归测试：改前在 ubuntu-26.04 红，
改后应绿；macOS（bash 3.2）两种情况都绿。
