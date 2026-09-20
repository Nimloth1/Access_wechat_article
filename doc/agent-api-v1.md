# Agent API / CLI V1

通过 PW 上已登录、未锁屏的交互桌面运行服务，其他 Agent 用 SSH 调用 CLI。服务复用现有文章采集后端，默认端口为 `127.0.0.1:8766`；与 GUI / dev_server 后端择一运行。先关闭现有后端，再启动本服务。不要同时启动其他操作微信的脚本或手动切换窗口。

## 启动与认证

在 PW 的用户环境中配置随机生成的 `AWA_AGENT_TOKEN`，确保桌面启动的服务和 SSH 客户端读取相同值。不要把 token 写进仓库、命令行参数或聊天记录。新设置的用户环境变量需要重新打开终端 / SSH 会话后生效。

首次可在 PW 桌面 PowerShell 中生成并保存（不会输出 token；已有 token 时不要重复生成）：

```powershell
[Environment]::SetEnvironmentVariable('AWA_AGENT_TOKEN', [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N'), 'User')
$env:AWA_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('AWA_AGENT_TOKEN', 'User')
```

在 PW 桌面终端执行（不能直接用普通 SSH 会话启动 UI 服务）：

```powershell
cd D:\projects\d\access_wechat_article
.\.venv\Scripts\python.exe awa_agent.py serve
```

服务仅绑定回环地址；所有端点（含 OpenAPI）要求 Bearer token，不支持浏览器跨源请求。客户端不需要安装 UI 自动化依赖。`health` 只验证服务可达，不表示微信控件或登录状态已验证。

## 其他 Agent 的调用方式

```text
ssh PW D:/projects/d/access_wechat_article/.venv/Scripts/python.exe D:/projects/d/access_wechat_article/awa_agent.py health
```

以下命令在 PW 项目目录执行，也可使用上述 SSH 形式和绝对路径：

```powershell
.\.venv\Scripts\python.exe awa_agent.py chat --chat "某个群" --date 2026-09-19 --max-pages 100 --scroll-pages 2 --idempotency-key chat-20260919 --wait
.\.venv\Scripts\python.exe awa_agent.py articles --account "某公众号" --limit 10 --archive-offline --idempotency-key articles-20260919 --wait
.\.venv\Scripts\python.exe awa_agent.py status JOB_ID
.\.venv\Scripts\python.exe awa_agent.py cancel JOB_ID
.\.venv\Scripts\python.exe awa_agent.py wait JOB_ID
.\.venv\Scripts\python.exe awa_agent.py accounts
.\.venv\Scripts\python.exe awa_agent.py saved-articles ACCOUNT_ID --page 1
```

不带 `--wait` 立即返回任务 ID。标准输出始终为 JSON；等待时任务 ID 另写标准错误，便于中断后查询。CLI 失败、超时或取消退出码为 1。中断客户端不取消任务，应显式调用 `cancel`。

## API

所有请求使用 `Authorization: Bearer <AWA_AGENT_TOKEN>`。提交时可传 `Idempotency-Key`：相同键和参数返回原任务，不同参数返回 409。已有任务占用桌面时返回 409，不排队。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent/v1/health` | 服务状态与活动任务 ID |
| POST | `/agent/v1/chat-jobs` | 群聊日期定位 |
| POST | `/agent/v1/article-jobs` | 公众号文章采集 |
| GET | `/agent/v1/jobs/{id}` | 查询任务和结果 |
| POST | `/agent/v1/jobs/{id}/cancel` | 请求取消 |
| GET | `/agent/v1/accounts` | 已归档公众号列表 |
| GET | `/agent/v1/accounts/{id}/articles?page=1&page_size=20` | 已保存文章链接、标题和归档路径 |

查询和取消也支持 `chat-jobs/{id}`、`article-jobs/{id}` 路径。完整参数见经过认证的 `/openapi.json`。

群聊请求：

```json
{"chat":"某个群","date":"2026-09-19","max_pages":100,"scroll_pages":2,"compatibility":false,"timeout_seconds":600}
```

群聊从当前可视位置向历史方向查找，展开时间标签核验日期；找到当天所在页后再向上滚动 `scroll_pages` 次。V1 不点击任意普通消息或下载附件，不返回聊天正文。返回 `found`、`reason` 和最终可视消息的摘要标识。`scan_limit` 表示预算用完，不能推断当天无消息；`date_before_current_position` 表示当前页面已早于目标日期，应先手动回到更近的消息。微信未确认历史起点或翻页衔接不可靠时会报告失败。`found=true` 表示滚动前定位到目标日期，额外滚动后目标可能离开屏幕。

公众号请求：

```json
{"account":"某公众号","limit":10,"collect_comments":false,"archive_offline":true,"skip_collected":true,"timeout_seconds":600}
```

V1 需要先手动打开目标公众号主页。服务核对主页名称后，从当前主页位置调用既有采集流程；名称不匹配即失败。暂不自动搜索公众号，也不保证存在足够文章达到 limit。结果保留主流程状态和统计；通过 accounts / articles 接口获取已保存文章的链接及 PW 归档目录，需要正文或离线文件时通过 SSH/SCP 读取对应目录。该列表包括以前保存的文章，不只本次任务。

## 生命周期与边界

状态为 `running → stopping → cancelled/timed_out`，或 `running → completed/failed`。取消和超时为协作式：底层清理完成之前仍占用桌面，不强杀微信。单次 timeout 范围为 1–7200 秒；底层未响应时可能超过该时长。关闭服务同样等待活动任务清理。

任务记录及幂等键仅保存在本进程内，重启后失效；文章归档仍持久保存。首次实机验证建议：先 health，再用小的 max_pages 定位一个已知日期，最后在已打开的公众号主页采集 1 篇。代理证书、微信控件可读性等前置条件与现有 GUI 采集相同。
