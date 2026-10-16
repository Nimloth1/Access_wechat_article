# 2026-10-15 末尾图片未定位：调查、治理与验证

机器：PW；时间为北京时间（UTC+08:00）。代码基线 `7c507cf`，开始前 fetch 后 ahead/behind 为 0/0。
目标群：学习新知识研究新思想💵，`51604620206@chatroom`。

## 事实与原因边界

夜间失败日志：
`I:\wechat_digest\logs\group_article_original_backfill\2026-10-15_51604620206_chatroom_images_20261016_023422.log`。
四项均为“未定位”，没有进入预览身份或图片过期判断。

更完整的现场在 `I:\wechat_digest\logs\todo10-validation\tail-diagnose.log`：
页面 61–63 停在“机构数据10月...更多 昨天 22:05”。末尾 UIA 子项为文字、19:19 图片、22:05 文字，
没有后续的目标图片行。viewport 为 `[511,182,1071,717]`，最后文字矩形为 `[511,642,1071,707]`。
`retry-four-no-early-stop.log` 显示取消静止退出后继续扫描，仍然未命中 22:05/22:59 图片。
因此单纯增加滚动次数无效；失败发生在日期搜索列表向 UIA 提供末尾消息这一层，早于图片行解析和预览校验。

**尚不能确定是微信查询结果没有加载末尾消息，还是 UIA provider 没有暴露它们。**
原现场没有可用的像素截图及内部查询结果对照。用户手动开图后，原逻辑也恢复可定位：本轮先只增加只定位与日志，
保留原扫描逻辑，已实际核验四项。故不能声称后面的修复已在原始失败状态上完成实机 A/B 验证。
也没有证据将本故障归因于锁屏、掉线或图片过期。

## 最小治理

只修改 `targeted_image_backfill.py`，并添加针对性测试：

- 单次扫描仍有目标未定位时，关闭旧聊天记录窗口，重新打开同一群并重建日期查询，再扫描一次。
  重试仅一次，保留未定位目标，不重复处理本轮已核验项；限额与取消时不重建查询。
- 添加 `--locate-only`，必须显式给出目标 ID 文件和诊断日志。该模式包含已是 original 的指定图片，
  仍实际打开预览，按原有完整 AutomationId 规则核验群、timestamp、local_id，反查导出 message_id。
  不点击“查看原图”，不写下载结果/过期尝试；未找到全部指定消息时抛错。
- 逐页诊断保留查询尝试编号、窗口、列表矩形、行名字/时间尾部和 AutomationId；
  正常模式的未定位诊断也增加停止原因、末页行、矩形及扫描页数。
- 原有身份核验、过期判断、落盘原图判定保持不变。
  外部包装脚本的最终 `pending > 0` 抛错与 `pending=0` 门禁未修改，也未用它的空待办结果代替定位验证。

这修复了脚本在同一个不完整列表上结束、缺少重建查询恢复步骤的问题。
重建查询能否恢复此次夜间故障的原始状态，仍需后续现场验证。

## 实际验证

在已完成原图的状态下运行只定位模式，**四项逐一打开预览并核验成功**：

| message_id | local_id | 时间 | 预览 AutomationId |
|---|---|---|---|
| 4186492 | 6368 | 22:05:50 | `PreviewWindow_51604620206@chatroom_1792073150_6368_0_0` |
| 4186493 | 6367 | 22:05:50 | `PreviewWindow_51604620206@chatroom_1792073150_6367_0_0` |
| 4186491 | 6369 | 22:05:51 | `PreviewWindow_51604620206@chatroom_1792073151_6369_0_0` |
| 4186490 | 6370 | 22:59:09 | `PreviewWindow_51604620206@chatroom_1792076349_6370_0_0` |

证据目录：`I:\wechat_digest\logs\group_article_original_backfill\`：

- `20261016_tail_baseline.jsonl`：保留旧扫描算法的实机验证，68 页，四项成功；说明现场状态已经变化。
- `20261016_tail_fixed.jsonl`：v2.8 在新建记录窗口上的实机验证，68 页；08:09:05–08:09:22 四项成功。
  本轮实机未触发重建分支，不能将它解释为该分支实机恢复成功。
- `20261016_tail_files_before.json` / `20261016_tail_files_after.json`：四份原图 DAT 和所用 recovery 日导出，
  共五个文件的大小、mtime_ns、SHA256 完全一致。脚本未写上游导出；下载尝试日志最后修改时间仍为 00:52:50。
- 回归测试模拟夜间“旧列表停在 22:05 文字、新查询出现图片”的现场，验证关闭并重建一次、
  预览身份精确匹配、持续失败保留缺口、限额/取消停止、原图及过期规则、只定位不下载。

本轮没有运行正式补抓/HC ping，没有以 CLI 跳过 original 或正式任务的 pending=0 作为修复证明。
验证创建的图片预览及记录窗口均已关闭。

自动检查：118 项测试、55 项子测试通过；`git diff --check` 通过。测试命令：

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_targeted_image_backfill.py tests/test_home_window_activation.py tests/test_wechat_chat_adapter.py tests/test_window_diagnostics.py tests/test_wechat_attachment_inventory.py -q
```

复现定位命令（会实际打开预览）：

```powershell
Set-Location 'D:\projects\d\access_wechat_article'
& '.\.venv\Scripts\python.exe' '.\targeted_image_backfill.py' `
  --chat '学习新知识研究新思想💵' --since '2026-10-15' `
  --target-ids-file 'I:\wechat_digest\logs\group_article_original_backfill\2026-10-15_51604620206_chatroom.txt' `
  --export-root 'I:\wechat_digest\source_recovery\historical_images' `
  --locate-only --diagnostic-log 'I:\wechat_digest\logs\group_article_original_backfill\tail_repeat.jsonl'
```

应出现四个 `preview_verified` 事件，message_id、local_id、AutomationId 均与上表一致；只看退出码不够。

## 未解决事项

原始缺图状态已被手动开图改变，无法在不改缓存的条件下重建；微信查询加载和 UIA provider 两者的精确责任尚未分清。
一次重建查询是有界恢复措施，其对该原始故障的因果有效性尚未实机证实。
再次出现时，应对照新的末页诊断和重建前后结果，不能继续无证据地解释为过期或桌面问题。
