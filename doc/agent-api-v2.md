# Agent API / CLI V2

V2 增加公众号自动打开、可见聊天消息读取与继续操作、归档正文读取。V1 路径和原命令保持兼容；认证、启动方式、桌面互斥与取消机制沿用 [V1](agent-api-v1.md)。服务必须运行在已登录且未锁屏的 PW 交互桌面，其他 Agent 可通过 SSH 调用客户端；不直接对外开放端口。

## CLI

以下在项目目录运行，输出为 JSON；远程调用使用 V1 文档中的绝对路径 SSH 形式。

```powershell
.\.venv\Scripts\python.exe awa_agent.py chat-v2 --chat "某个群" --date 2026-09-19 --max-pages 30 --wait
.\.venv\Scripts\python.exe awa_agent.py chat-action --chat "某个群" --action read --wait
.\.venv\Scripts\python.exe awa_agent.py chat-action --chat "某个群" --action scroll --page-token PAGE_TOKEN --scroll-pages 1 --wait
.\.venv\Scripts\python.exe awa_agent.py chat-action --chat "某个群" --action click --page-token PAGE_TOKEN --message-id MESSAGE_ID --wait
.\.venv\Scripts\python.exe awa_agent.py articles-v2 --account "某公众号" --limit 1 --wait
.\.venv\Scripts\python.exe awa_agent.py accounts
.\.venv\Scripts\python.exe awa_agent.py saved-articles ACCOUNT_ID
.\.venv\Scripts\python.exe awa_agent.py content ARTICLE_ID
```

`PAGE_TOKEN` 和 `MESSAGE_ID` 必须替换为前一任务 `result.page_token` 和 `result.visible_messages[].id`。可复用 `status`、`wait`、`cancel` 查询任何版本的任务。提交命令均支持 `--idempotency-key` 和 `--timeout-seconds`；推荐点击使用唯一幂等键，网络重试沿用该键。

## API

所有端点仍要求 `Authorization: Bearer <AWA_AGENT_TOKEN>`。任务提交可带 `Idempotency-Key`；桌面忙返回 409，参数不合法返回 422。执行期间页面变化等错误记录为任务 `failed`，客户端应检查任务终态，不能只检查提交的 202。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/agent/v2/chat-jobs` | 与 V1 相同参数，增加返回可见文本、消息 ID、页面凭据 |
| POST | `/agent/v2/chat-actions` | 当前群可见消息读取、历史滚动、单击 |
| POST | `/agent/v2/article-jobs` | 与 V1 相同参数，新增 `open_account`，默认 true |
| GET | `/agent/v2/articles/{id}/content` | 已归档正文纯文本 |
| GET | `/agent/v2/health` | 服务状态，不代表微信就绪 |
| GET / POST | `/agent/v2/jobs/{id}` / `/agent/v2/jobs/{id}/cancel` | 查询 / 取消 |
| GET | `/agent/v2/accounts`、`/agent/v2/accounts/{id}/articles` | 原归档列表，支持原分页参数 |

聊天动作请求示例：

```json
{"chat":"某个群","action":"read","timeout_seconds":600}
```

`action` 可为 `read`、`scroll`、`click`。后两项必须带上次返回的 `page_token`；`click` 还必须带 `message_id`。`scroll_pages` 默认 1，范围 1–100；只向更早消息滚动。`compatibility` 默认 false。

## 边界与安全约束

- `chat-v2` 仍从当前可视位置向历史定位日期，不支持跳转到比当前更晚的日期。返回的是最终可见页，不是当天全部聊天记录。
- 消息 `text` 来自 UIA 行的可读文本，可能包含发送者和附件标签，不保证是纯正文；未暴露给 UIA 的内容不进行 OCR 或猜测。日期无法确定时返回 null。独立的继续操作不继承前一次任务的日期缓存。
- 消息 ID 由当前窗口、群名、运行时控件实例及内容生成，不是微信永久 ID。页面凭据包含消息顺序和几何位置；滚动、布局重建、切群后应重新读取。继续操作只核对当前群，不自动搜索切群。取消后也应重新读取。
- 点击只在重新核验的气泡上执行一次单击，不保证打开详情。拒绝文件消息，避免启动关联程序；拒绝无唯一可见气泡、气泡中心不在视口、页面已变化的情况。不发送消息、不转账、不自动确认后续弹窗。链接、图片等可能打开查看窗口，需关闭后重新读取聊天页。
- 公众号自动导航使用微信本地搜索精确匹配账号会话（通常是已关注且可搜索的账号），然后查找明确的历史消息入口，必要时先点账号标题。不是全网“搜一搜”，也不自动关注账号；重名、控件不可读、入口不存在时明确失败。打开后再次核对主页名称，匹配才采集。`articles-v2 --already-open` 或 API `open_account:false` 可复用手动主页。
- 正文由归档的 `origin/main.html` 中 `js_content` 提取，过滤脚本和样式，不返回整个页面或认证信息。不执行 HTML，不请求外部资源，不提取图片文字。纯媒体正文可能为空并返回 `text_empty:true`；微信文字分享页使用已有标题提取规则。不存在/未归档返回 404，未识别正文/编码错误返回 422，源文件大于 10 MiB 返回 413。
- 任务与幂等记录仍为进程内存态，重启失效；正文和文章目录为持久化归档。不同版本共享同一桌面锁，不支持并行 UI 操作。

## 验证

自动测试覆盖请求校验、版本兼容、互斥、取消、超时、页面与消息校验、账号复核、正文过滤和路径边界。测试使用模拟控件，不能替代实际微信版本验收。

实机建议顺序：定位一个已知日期 → 读取当前页 → 滚动一页 → 用过期凭据确认拒绝 → 用新凭据单击一条普通文字消息 → 自动打开已关注公众号采集一篇 → 按文章 ID 读取正文。进行实机测试前确认没有其他人或脚本操作该桌面。
