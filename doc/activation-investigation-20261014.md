# PW 原图补抓夜间点击失败调查（2026-10-14）

调查机器：PW / DESKTOP-V8117LP。时间均为 UTC+08:00。
调查对象：2026-10-14 02:30 的 daily-original-backfill；目标日期 2026-10-13。
代码基线：457bf26，main 与 origin/main 同步后开始调查。

## 2026-10-14 后续实现：自动记录受阻现场

已按用户要求补充取证日志；原来的坐标归属核验、停止条件和一次 force 重试保持不变。
`src/modules/window/window_diagnostics.py` 直接读取原始 HWND，不通过过滤窗口标题的枚举器。
首次受阻就输出 `[window-diagnostic] ` 前缀的一行 JSON；即使 force 后成功，首次现场仍保留。
定向补图与适配器点击路径分别标记 `before_force` / `after_force`；其他坐标核验使用 `point_check`。
输入桌面不可访问或激活后没有前台窗口时也记录诊断，前者保留实际失败 API 的错误码和访问权限。

每条记录包含时间、点击点、原始遮挡句柄、采样时命中句柄、目标及前台句柄。
相关窗口（含根窗和直接 owner）记录有效性、PID、线程、进程名/路径/启动时间、
标题、类名、矩形、可见/最小化状态、扩展样式、TOPMOST/TOOLWINDOW/NOACTIVATE、DPI、
窗口线程桌面及进程会话 ID。另记录采集线程桌面、输入桌面、当前会话连接状态及锁屏标志、
当前线程 DPI awareness 与虚拟屏幕范围。读取失败以 null 和 `errors` 保留，
窗口已经失效则明确记录 `valid=false`，不会将后续命中的窗口误归因为原始遮挡。

日志写入标准输出，现有夜间包装脚本已将其落到
`I:\wechat_digest\logs\group_article_original_backfill\*_images_*.log`，无需修改调度或 HC。
检索示例：

```powershell
rg '\[window-diagnostic\]' 'I:\wechat_digest\logs\group_article_original_backfill' -g '*.log'
```

只读实机采样已成功得到 `Default` 输入/线程桌面、会话 3、`state=0`、`flags=1`、
`locked=false`，且诊断错误为空。此验证没有激活或点击微信。
锁屏和无标题遮挡的失败路径由模拟 API 测试验证；尚未用一次真实夜间失败验证新日志。
下文的 TOPMOST 回退及更严格桌面前置校验仍为建议，本次未实施。

## 结论与边界

1. **已确认 force 路径执行，但它不等于真正 TOPMOST。** 原始堆栈先在
   `wechat_chat_adapter.py:353` 校验失败，再执行 357 行 force，最后 358 行仍失败。
   八个普通图片批次都命中同一个 7147088，坐标均为 (231, 57)，目标为 655860。
   这不是某一个群的内容问题。
2. **已用真实 Windows API 验证能力边界。** 自建的 TOPMOST 遮挡窗盖住自建目标窗时，
   现有 `WindowsHomeWindowGuard.activate(force=True)` 已把目标变成前台，
   但 `ensure_target_clickable()` 仍失败。把目标升为 TOPMOST 后通过；
   在点击前降为 NOTOPMOST，遮挡立即回来。因此“TOPMOST 一下马上取消”不能保证后续点击。
3. **7147088 的历史身份尚未证实。** 23:00 和 23:09 的 `IsWindow(7147088)` 都为 false。
   凌晨日志没有保存它的 PID、进程、类名、标题、exstyle、桌面名，不能恢复这些字段。
   不能据此断言是 PowerToys、输入法、远控、Explorer 或另一个微信窗口。
4. **夜间锁屏或输入桌面切换是优先调查假设，普通 TOPMOST 遮挡仍是候选。**
   Winlogon Operational 在 10/13 22:33:26.592 记录 TermSrv 通知值 4，
   下一次值 5 在 10/14 08:36:04.096；02:30 任务处于两者之间。
   该序列与锁定/解锁假设一致，但本轮未独立校准这两个通知数值的含义，
   也没有读取到 Security 4800/4801（当前令牌无读取权限）。用户回答“没有，或记不清”，
   不能用作锁屏确认。**此项是推断，不是已证实根因。**
5. 建议先补失效现场的直接元数据与桌面状态检查，再决定是否增加 TOPMOST 回退。
   本轮没有改业务逻辑、调度、安全设置、Healthchecks，也没有启动整批补抓。

## 证据

| 证据 | 路径或内容 |
| --- | --- |
| 凌晨主日志 | `I:\wechat_digest\logs\group_articles_original_backfill_20261014_023004.log` |
| 样例子日志 | `I:\wechat_digest\logs\group_article_original_backfill\2026-10-13_21177687256_chatroom_images_20261014_023511.log` |
| 当时的缺口计划 | `I:\wechat_digest\logs\group_article_original_backfill\2026-10-13_plan.json`：44 个缺口，42 图片、2 转发记录；仅代表计划保存时状态 |
| 任务启动/结束事件 | `I:\wechat_digest\logs\activation_investigation_20261014\task_night.json` |
| 夜间 Winlogon 通知 | 同目录 `winlogon_night.json` |
| 最初窗口快照 | 同目录 `snapshot_initial.json` |
| 修正输出并加入 WTS 会话标志后的快照 | 同目录 `snapshot_final.json` |
| 自建窗口实验及结果 | 同目录 `topmost_probe.py`、`topmost_probe_result.txt` |
| 可重复的只读诊断脚本 | 同目录 `window_snapshot.ps1` |

调度事件：02:30:01.291 触发，02:40:02.808 动作结束，返回码 2147942401
（0x80070001）。事件描述中的“successfully completed”表示动作已结束，不能当作业务成功。
任务 principal 为 Interactive、Highest，用户 maegl。

23:09 快照：当前 SessionId=3，WTS 信息为 `[Level=1, SessionId=3, State=0, Flags=1]`，
即连接状态 Active、会话已解锁；输入桌面与当前线程桌面均为 Default。
微信 655860 为 Weixin.exe / Qt51514QWindowIcon，PID 50216，
进程自 10/13 09:40:53 起运行，visible=true、iconic=false、exstyle=0x00000100，
没有 TOPMOST；快照坐标为 (1556,0,2916,724)。实验前后该位置和样式一致。

当前两块显示器，DPI-unaware 的 PowerShell 快照虚拟桌面为 (0,0,3360,2560)。
快照中的可见 TOPMOST 窗包括 wetype 输入法状态栏与 Explorer 任务栏，
没有覆盖当前微信左上搜索区。当前旧坐标 (231,57) 属于 Excel 子窗口；
这只说明桌面布局已经变化，不能将现在的 Excel 追认为凌晨的遮挡进程。
当前 Explorer 进程创建时间为 10/14 22:14 和 22:49，夜间原窗口无法靠现在的枚举追溯。

## 为什么现有 force 会无效

- `home_window_activation.activate()` 调用 UIA SetActive、BringWindowToTop、
  SetForegroundWindow，必要时 AttachThreadInput。force 只跳过“前台已属于主页”的早退，
  没有 SetWindowPos(HWND_TOPMOST)。真实实验已证明前台归属正确也可能被 TOPMOST 覆盖。
- `wechat_chat_adapter._activate()` 只检查 OpenInputDesktop 是否返回非空，以及
  GetForegroundWindow 是否非零；没有核对输入桌面名、WTS 锁定标志，
  也没有要求前台归属微信。任务使用 Highest，但这次没有在相同权限的锁定状态下实测，
  不能断言其能打开哪一个桌面。可以确认的是“打开桌面句柄成功”不包含桌面身份校验。
- 错误报告依赖 `enumerate_desktop_windows()`；Win32 枚举会跳过
  “标题为空、且进程名不包含 wechat/weixin”的窗口，进程查询失败也会返回空名称。
  其他桌面上的窗口也不一定在此枚举中。因而“不可识别”不能等同于没有窗口。
- force 后重新绑定窗口，但第二次校验复用此前的 x/y；没有重新读取控件矩形。
  若重激活期间窗口移动或布局变化，重试仍可能检查旧点。这一机制存在，凌晨是否触发未知。

微软说明普通前台激活存在策略限制，TOPMOST 与普通层级也不同：
[SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)、
[SetWindowPos](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos)。
输入桌面是独立对象，安全桌面切换不能靠提升 Default 桌面的窗口解决：
[Desktops](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops)、
[OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop)。

## 根因假设排序

| 假设 | 可信程度与验证缺口 |
| --- | --- |
| 夜间锁屏 / 输入桌面切换 | 首要、但未证实；Winlogon 夜间序列、前台不在枚举中与跨群失败相符。缺 Security 事件或当时的 WTS/桌面快照 |
| 无标题 TOPMOST 窗覆盖 | 机制已实测；历史归因未知。缺 7147088 的 exstyle、PID、class、rect |
| 前台策略限制 | 可能；代码忽略激活返回值，调用方接受任意非零前台。缺 force 前后的真实前台句柄和 API 返回值 |
| 混合 DPI / 坐标旧值 / 微信在另一显示器 | 可能但无直接夜间证据；当前布局已变化，不能拿当前坐标证明凌晨越界 |
| 远控或 Deskflow 注入、其他微信窗、通知/输入法 | 较弱；仅发现相关驻留进程不能构成因果证据。Deskflow 当前隐藏窗只有 1×1，当前输入法状态栏也不在搜索区 |

本轮未发现 02:15–02:45 的 System 或 TerminalServices LocalSessionManager 事件。
没有事件不代表未发生桌面切换；Security 日志读取被拒绝。

## 最小补丁草图（尚未实施）

1. 在第一次遮挡以及 force 后失败时，直接按 HWND 查询前台、命中窗、root、owner 的
   PID/process/class/title/rect/exstyle/visible/iconic/thread/desktop，不依赖有标题的候选列表。
   加入 WTS 会话状态、输入/线程桌面名、当前 DPI context、目标及控件矩形，
   并记录激活 API 返回值；写入本地失败日志，不发送窗口标题到外部监控。
2. 在任何输入前检查 WTS SessionFlags、输入桌面与目标线程桌面。
   锁定、会话不可用或桌面不一致时明确停止；不尝试切换安全桌面或自动解锁。
   WTS 的 LOCK=0、UNLOCK=1 对本机 Windows 10 适用：
   [WTSINFOEX_LEVEL1_W](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)。
3. 对普通激活要求最终前台属于目标窗；force 后重新读取控件和矩形，再核验坐标。
   失败只重试一次，继续保留点击点归属保护。
4. **仅当现场确认是同桌面的普通 TOPMOST 遮挡时**，再考虑围绕一次
   “核验→点击”的小作用域将目标临时设为 TOPMOST，并在 finally 恢复原本层级。
   不能在核验成功后、点击发生前就取消。不要默认最小化不属于本任务的窗口。
   风险包括抢焦点、弹层焦点改变、连带影响 owned 窗口、退出前意外保留置顶；
   也不能解决锁屏、DPI 错点或其他程序再次抢占。

## 本地重复验证

只读取证（约 6 秒；不激活、不点击、不写 Healthchecks；JSON 会保留本机窗口标题）：

```powershell
& 'I:\wechat_digest\logs\activation_investigation_20261014\window_snapshot.ps1' -OutputPath 'I:\wechat_digest\logs\activation_investigation_20261014\snapshot_repeat.json'
```

建议在下一次失败现场运行，尤其保存 WTS Flags、桌面名和命中 HWND 元数据。
脚本中的 655860、7147088 是此次事件句柄；若已更换，应先修改为新日志中的值。
脚本在独立 PowerShell 进程内运行；它的 DPI 状态是诊断进程的状态，
不能替代失败 Python 线程的 DPI 状态。业务内应在原失败线程直接采样。

真实 TOPMOST 能力实验（仅创建并销毁两个测试窗，不操作微信）：

```powershell
& 'D:\projects\d\access_wechat_article\.venv\Scripts\python.exe' 'I:\wechat_digest\logs\activation_investigation_20261014\topmost_probe.py'
```

退出码 0、`probe_windows_destroyed=true`，且四阶段 clickable 为
`false → false → true → false` 才算验证通过。本轮已得到该结果，
force 后 foreground=目标而 hit=遮挡；实验窗口均已销毁，原前台已恢复。

单群单消息补抓命令（会短暂操作微信和写原图/本地尝试日志；不经过 HC 包装）：

```powershell
Set-Location 'D:\projects\d\access_wechat_article'
& '.\.venv\Scripts\python.exe' '.\targeted_image_backfill.py' --chat '平安养老-国泰基金交流群' --export-chat '平安养老-国泰基金交流群' --since '2026-10-13' --max-items 1 --target-ids-file 'I:\wechat_digest\logs\group_article_original_backfill\2026-10-13_21177687256_chatroom.txt' --export-root 'I:\wechat_digest\source_recovery\historical_images'
```

先确保会话解锁、微信在输入桌面、没有外部遮挡。该命令自身启用并恢复项目已有的临时兼容读取。
本轮未执行此补抓命令；不应把它当成已验证的补抓成功。
退出码 0、没有 AttachmentNotClickableError，表示这次 GUI 路径未因遮挡中断；
最终原图必须通过落盘清单状态变为 original 核验。
若没有待补项，脚本会直接退出，不能用这种退出码证明搜索点击已验证；
图片过期或尚未定位也不能算原图补齐成功。

## 下一步建议

先在可交互桌面上做上述单消息验证，再考虑补跑 TargetDate=2026-10-13。
当前没有可归因的遮挡窗，不能指定一个应用让用户关闭。
若要整批补跑但保持 HC 只读，包装脚本必须显式使用 `-Inner`，否则会发送 HC ping：

```powershell
& 'D:\projects\d\wechat_digest\tools\run_pw_group_articles_original_backfill.ps1' -Inner -TargetDate '2026-10-13'
```

整批成功应出现 `stage=complete ... pending=0 image_pending=0 record_pending=0`；
这不会自动把 HC 转绿。HC 变绿属于恢复正常受监控运行后的结果，本轮不执行。
一次白天补跑成功也不能证明夜间问题修复。优先实施取证与桌面前置校验，
有现场证据再选择环境处理或 TOPMOST 回退；不建议直接合入永久置顶。
