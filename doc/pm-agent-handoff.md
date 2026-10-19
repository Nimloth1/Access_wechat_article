# PM Agent → PW AWA 能力与交接说明

核验日期：2026-10-12（Asia/Singapore）。本文描述 PW 当前部署的 V2.1 能力；origin/main 不等于 PW 已部署代码，不要从远端主分支覆盖本机服务。

## 用途和入口

当 PM 上 WeRSS 无头采集拿不到微信公众号正文时，可调用 PW 的 Access_WeChat_Article（AWA），利用已登录的微信 PC 和本机 MITM 保存文章。PW 负责采集和交接，本文不授权修改 PM / WeRSS 或回填数据库。

| 项目 | 值 |
| --- | --- |
| 主机 | PW / DESKTOP-V8117LP，用户 maegl |
| SSH | PW；连接失败时尝试 PWT |
| 已部署项目 | `D:\projects\d\access_wechat_article` |
| Python | `D:\projects\d\access_wechat_article\.venv\Scripts\python.exe` |
| 客户端 | 项目内 `awa_agent.py` |
| 本机服务 | `127.0.0.1:8766`，仅回环监听 |
| 认证 | PW 用户环境变量 `AWA_AGENT_TOKEN` |
| 归档根目录 | `D:\projects\d\access_wechat_article\storages` |

完整接口：[V1：认证、生命周期、互斥](agent-api-v1.md)、[V2：自动导航、聊天动作、正文](agent-api-v2.md)。跨机使用 SSH 在 PW 执行客户端，无需将 8766 对外开放，也无需把 token 复制到 PM。

## 能做什么

| 能力 | 调用 | 边界 |
| --- | --- | --- |
| 服务健康与占用查询 | `health` | ready 只代表服务可达，不代表微信可操作 |
| 微信就绪诊断 | `diagnose` | 看 `wechat_ready` / `search_edit_count` / `main_window_count`；忙时 409，未就绪 CLI 退出码 1 |
| 搜索公众号并从主页采集 | `articles-v2 --account ... --limit ...` | 按账号、可见主页采集；没有提交指定 URL 的公共接口 |
| 复用已打开主页 | 上述命令增加 `--already-open` | 仍会核对主页账号 |
| 查看归档账号、文章、路径 | `accounts`、`saved-articles ACCOUNT_ID --page 1` | 列表包含旧归档，需分页及按 URL 核对 |
| 读取标准归档正文 | `content ARTICLE_ID` | 非空正文才算验证成功；图片页另见下文 |
| 查询、等待、取消任务 | `status JOB_ID`、`wait JOB_ID`、`cancel JOB_ID` | 中断 SSH/客户端不会自动取消任务 |
| 聊天日期定位与可见消息动作 | `chat-v2`、`chat-action` | API 提供，未作为本轮公众号采集的实机验收项；不是完整聊天导出 |

2026-10-11 批次实机已自动打开 13 个公众号主页；新增归档 269 篇，96 个缺正文目标取得 50 个（45 个新抓、5 个旧归档），46 个未取得。2026-10-12 的两次服务重启后，404K 标准两篇命令均 completed，额外重抓两篇正文验证成功。不能保证账号主页展示所有目标，也不能仅凭未找到就判定文章已删除。

## PM 上调用的最小示例

先读 PM 的 `~/.codex/context/machines.md`，核对 SSH 别名。下面在 PM 的 Unix shell 执行，最终解释器是 PW Windows PowerShell。使用 UTF-16LE `-EncodedCommand` 避免中文、路径和跨 shell 引号问题。

```bash
python3 - <<'PY'
import base64
import subprocess

script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:AWA_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('AWA_AGENT_TOKEN', 'User')
$env:PYTHONIOENCODING = 'utf-8'
if (-not $env:AWA_AGENT_TOKEN) { throw 'PW 未配置 AWA_AGENT_TOKEN' }
Set-Location -LiteralPath 'D:\projects\d\access_wechat_article'
$taskAwaArgs = @('diagnose')
& '.\.venv\Scripts\python.exe' '.\awa_agent.py' @taskAwaArgs
exit $LASTEXITCODE
"""
encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                'PW', 'powershell.exe', '-NoProfile',
                '-EncodedCommand', encoded], check=True)
PY
```

依次替换示例的 `$taskAwaArgs`，每次只运行一个命令：

```powershell
# 先小批量验证；未带 --wait 时提交后立即返回任务 ID。
$taskAwaArgs = @('health')
$taskAwaArgs = @('diagnose', '--accessibility', 'auto')
$taskAwaArgs = @('articles-v2', '--account', '404K', '--limit', '2', '--timeout-seconds', '900', '--idempotency-key', 'pm-404k-20261011-probe')
$taskAwaArgs = @('status', 'JOB_ID')
$taskAwaArgs = @('wait', 'JOB_ID')
$taskAwaArgs = @('cancel', 'JOB_ID')
$taskAwaArgs = @('accounts')
$taskAwaArgs = @('saved-articles', 'ACCOUNT_ID', '--page', '1')
$taskAwaArgs = @('content', 'ARTICLE_ID')
```

`JOB_ID` 是任务响应的 `id`；`ACCOUNT_ID`、`ARTICLE_ID` 必须用实际列表返回值。`status` 必须带任务 ID。先确认 health 没有 active_job，再确认 diagnose 的 `wechat_ready=true`、`main_window_count=1`、`search_edit_count=1`。重试同一次提交沿用幂等键；新批次使用新键。已有任务时返回 409，服务不排队。不要只看到提交成功就认定正文已取得，需检查任务终态与文章正文。

## 桌面条件和当前限制

- 服务必须在 PW 已登录、未锁屏的交互桌面启动；普通 SSH 会话可调用客户端，不能用来直接启动桌面服务。微信需要登录，UIA 需要读到搜索框；只有空壳 `MMUIRenderSubWindowHW` 不算就绪。
- 所有桌面操作串行，不要并行提交账号，不要同时运行 GUI 后端或其他微信自动化。任务取消后，等待清理完成再提交下一批。
- V2.1 已固化：serve 主线程 MTA 与 UIA 初始化；公众号任务默认 `accessibility=auto` 兼容读屏；最小化主窗口恢复；搜索后独立账号窗口名称/进程核验及“公众号主页”入口；采集前主页账号复核；独立 diagnose 就绪检查。CLI 可用 `--accessibility off|on` 显式选择。兼容标志在正常、失败或取消清理时恢复，仅支持已核验微信 4.1.12.55 映像；其他版本不自动写标志。
- PW 新服务入口为 `start_awa_agent.ps1`，计划任务 `AWA_Agent_Service` 以 maegl 交互会话登录时启动并有限重试。恢复和日常运行不再需要手工 `python -c` / 常驻兼容开关。诊断不恢复最小化窗口、不输入搜索；任务会先激活并重新核对控件。
- 仍需桌面维护的情况：Windows 锁屏/未登录、微信退出或要求重新登录、窗口未出现、微信升级导致映像未知。此时 PM skip 并报告 diagnose 的 detail。就绪只证明搜索能力，不保证目标号可搜索、主页文章覆盖完整或所有媒体都有文字。
- `articles-v2` 自动导航若失败，不能假装成功或直接复用未经核验的主页。记录失败原因，交由 PW 桌面维护流程恢复；不要要求用户手动导航作为默认步骤。
- 标准 `content` API 不支持本轮的 type8 图片文章。5 篇特殊归档保存了未经修改的 `origin/main.html` 和真实说明文字 `body-caption.txt`。API 返回 422 不代表这些文件没有正文；按交接 JSON 的 `body_caption_path` 读取。不能把标题当正文，也未对图片做 OCR。
- AWA 任务记录与幂等键只保存在服务进程内存，重启会丢失；文章归档持久保存。PM 已部署的 launchd 排队/退避属于 PM 调度层，AWA 本身仍不提供持久化失败队列或直接提交指定 URL 的公共接口。

## 获取现有交接资料与正文

本轮交接目录：`D:\projects\d\access_wechat_article\output\weress-capture-20261011`。

新恢复复测及最新 52 篇队列补抓目录：`D:\projects\d\access_wechat_article\output\weress-capture-20261012`。读取该目录 `handoff.json`、`targets-52.csv`、`report.md`；旧目录用于历史对照，不代表当前 WeRSS 缺失清单。新交接逐目标包含 `werss_id`、`awa_id`、`awa_url`、`archive_path`、`html_path`、`body_caption_path`（若有）、`match_basis`、`body_valid`。`captured` 为本轮新增匹配，`existing` 为旧归档验证可用；`not_obtained` / `body_empty` 不计正文成功。已有归档也需 PM 确认尚未回填，匹配需核对。

| 文件 | 内容 |
| --- | --- |
| `handoff.json` | 96 个目标结果、269 个新增归档、账号统计、匹配依据、路径 |
| `targets-96.csv` | WeRSS 原 ID/URL/标题、对应 AWA 结果及失败原因 |
| `new-articles.csv` | 新增归档列表与正文验证 |
| `accounts-summary.csv` | 每账号目标取得及未取得数 |
| `report.md` | 人类可读汇总 |

在 PM 的已授权交接目录取清单，例如（目标文件写到当前目录）：

```bash
scp PW:D:/projects/d/access_wechat_article/output/weress-capture-20261011/handoff.json ./awa-handoff-20261011.json
```

`saved-articles` 的文章字段为 `id`、`title`、`articleLink`、`publishedArticleTime`、`archiveDir`。普通正文优先用 `content`；需原 HTML 或图片说明时，按返回的归档路径通过 SSH/SCP 获取 `origin/main.html` / `body-caption.txt`。中文、空格路径建议转换为正斜线，并由 Python `subprocess.run` 参数列表调用 SCP，避免拼接 shell 命令。

原始 `origin/request.json` 和 HTML 可能包含微信会话证据。不要在聊天、报告或公共仓库发布整份原始请求、cookies、临时密钥或 token；交接优先传必要正文及公开文章元数据。

匹配优先使用账号 + 公开文章 URL。此次 WeRSS 部分短链的 `~` 与微信打开的 `_` 对应，仅用于本轮本地匹配，不可当作通用 URL 改写规则。标题/日期回退匹配需复核；保留双方原 URL 和 `match_basis`，不能直接据此覆盖 PM 数据。回填另需明确授权。

## 后续补抓请求应提供什么

提供每篇的 WeRSS ID、账号名称、原始 URL、标题、发布时间、PM 的失败原因和失败时间，再注明账号优先级、扫描上限、是否保存离线资源，以及仅归档还是另有回填授权。无需提供 token。

这是一份请求格式示例，不是现有可提交的队列 API：

```json
{
  "source": "PM/WeRSS",
  "mode": "archive_only",
  "targets": [{
    "werss_id": "实际文章 ID",
    "account": "404K",
    "url": "实际微信公众号 URL",
    "title": "实际标题",
    "publish_time": "实际发布时间",
    "failure_reason": "实际错误信息",
    "failed_at": "实际失败时间"
  }]
}
```

可给 PM Agent 的指令：

> 先通过 SSH 读取 PW 的 `D:/projects/d/access_wechat_article/doc/pm-agent-handoff.md`，按其客户端示例检查 health、diagnose 和现有归档。PW AWA 可从微信 PC 公众号主页补抓；先每账号 2 篇验证，再串行扩大。不要把 AWA 未提供的队列或指定 URL 接口当成已存在功能。此次只采集归档、输出交接结果，不修改 WeRSS、不回填。


## 2026-10-19 锁屏公众号实验：未通过，未部署

实验分支保留 `serve --window-messages`、会话/进程核验、活动渲染控件绑定、
执行线程重绑文章控件、UIA/MSAA 动作和浏览器内部焦点处理。91 项相关测试及
10 个子测试通过；只读和短实验确认锁屏不变、导航可完成、当前页可从多个旧页
中分离、渲染焦点可在不改变锁屏前台的情况下设置。

正式原 wrapper 有界单号测试中，跳过既有归档的一轮只完成了扫描，不能算新采集。
显式重采一篇的窗口消息、UIA Invoke、MSAA 及内部焦点方案均未通过文章标签
打开验收；最终 20261019_134319 报告仍失败、回填 0。未确认是否还有文章状态、
浏览器处理或标签检测层原因，不能声称锁屏采集不可实现，也不能声称已修好。

实验代码只存独立分支；main 保留原启动方式，不启用未验收模式。后续须先取得
一次真实打开/关闭/归档证据，再考虑部署，禁止因单测或扫描成功恢复声明。
已观察到 WeChatAppEx 未开启本机调试监听；本轮未重启微信客户端或改其启动参数。
PM 权威证据：feishu-healthchecks/awa-dispatch-repair-20261019/。
