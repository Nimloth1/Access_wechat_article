# 夜间关屏诊断

一次性监控 PW 的当前交互桌面。默认时段为新加坡时间 2026-10-17 23:00 至 2026-10-18 09:00。

## 安装与输出

运行 `windows/install-screen-idle-task.ps1`，创建 `Codex-Screen-Idle-20261017` 一次性计划任务。
在管理员 PowerShell 中运行安装器时，任务可以每分钟读取 `powercfg /requests`；普通权限安装时，其他记录照常采集，并明确标注缺少进程级电源请求。
任务使用当前用户的交互登录会话，不保存密码；需要用户保持登录。它不唤醒睡眠中的电脑，错过启动时会在可用后补启动，但超过结束时间不会继续采集。

输出：`data/screen-idle/20261017-230000/`。

- `timeline.jsonl`：每五秒记录当前会话最后输入时间、空闲时长、前台进程名/PID、系统电源状态；实时记录控制台/会话显示开关、变暗和会话切换事件。
- `deskflow` 事件：跟随 `C:\ProgramData\Deskflow\deskflow-daemon.log`，仅保留进入/离开电脑及相关状态事件。日志文件被替换时可能存在缺口，不能以缺少事件证明没有切换。
- `power_requests` 事件：管理员模式下每分钟以及检测到电源请求状态变化时保存原始请求和退出码。
- `processes` 事件：每分钟保存相关进程名、PID 和会话 ID，不保存命令行或窗口标题。
- `start-*` / `end-*`：开始与结束时的显示/睡眠电源设置，包括隐藏的锁屏关屏超时。
- `summary.json`：运行状态、覆盖时段、样本计数及初步统计，每分钟更新；完整归因需对照时间线。

监控只读，不改变关屏、睡眠、锁屏设置，不模拟输入、不截图、不录音、不记录键盘内容，也不持有保持唤醒请求。
Windows 报告“显示关闭”并不能证明物理显示器背光已经熄灭；后者需要现场观察或外部设备验证。
显示事件值：0 为关闭，1 为开启，2 为变暗；首次通知仅表示当前状态。会话事件值：7 为锁屏，8 为解锁，其余值遵循 Windows WTS_SESSION_CHANGE。
`GetLastInputInfo` 只能给出当前会话的输入时间，无法可靠识别输入来源。Deskflow 进入事件和随后计时变化只能提供相关性；前台进程不是输入发起者证据。
短暂的电源请求或小于五秒的输入变化可能被采样漏掉。无输入而持续亮屏时，还需结合显示请求、Deskflow 日志和显示驱动排查，不能自动断言为某个程序。

## 手动验证与停止

短时测试可传入带时区的 `-StartAt` 和 `-EndAt`，并使用独立 `-OutputDirectory`。测试不会主动关闭显示器或锁屏。

运行中在本次输出目录创建名为 `STOP` 的空文件，可在下一采样周期正常退出。
开始前可执行 `Disable-ScheduledTask -TaskName 'Codex-Screen-Idle-20261017'`；结束后可用 `Unregister-ScheduledTask -TaskName 'Codex-Screen-Idle-20261017' -Confirm:$false` 移除任务。日志保留供分析。

Windows API 参考：[电源事件订阅](https://learn.microsoft.com/windows/win32/power/registering-for-power-events)、[输入空闲时间](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getlastinputinfo)、[一次性重置关屏计时](https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate)。
