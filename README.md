# hermes-ops-lite

Hermes Agent 的工具输出层插件：进上下文之前先收敛，长结果留档，重复读不重发，失败的写操作标出效果三态。

## 安装

```bash
git clone <this repo> ~/.hermes/plugins/ops-lite   # 或复制到 %LOCALAPPDATA%\hermes\plugins\ops-lite
hermes plugins enable ops-lite
# 改完插件代码要让新代码生效：重启后端（桌面端会自己把 serve 进程拉回来）
```

配置写在 `config.yaml` → `plugins.entries.ops-lite`，全部可省。

## 测试

```bash
cd tests
python unit_effect.py        # 效果判定：34 项
python unit_ops_lite.py      # 收敛/留档/预算：21 项
python unit_retrieval.py     # 取回侦测端到端：8 项
python stress_effect.py 300  # 压力：随机 300 轮 + 定点 + 并发 + 热路径
python fuzz_rounds.py 51     # 模糊不变量：51 轮
python analyze_ledger.py [savings.jsonl] [state.db]   # 收益表（只读）
```

测试默认从仓库根目录找插件文件，不需要改路径。

## 致谢与出处

本项目是 [**dsh-ops**](https://github.com/T-Auto/dsh-ops)（作者 **T-Auto**，MIT AND Apache-2.0）的**思路移植**：
不是它的分支，也没有复制它的代码。原项目是 DSH（DeepSeek Harness）的原生工具插件，
这里按 Hermes 的 `transform_tool_result` 钩子重新实现。

**建议先去看原仓库**，下面几处比这里做得更深：

- `native/crates/ops-results`：有界输出的 capture → spill → `ref://` 引用与分页（带 contentHash、complete、omitted），比"截断"完整得多
- `src/kernel/` 与 `Failure.effectState`：失败语义三态（none / applied / unknown）+ 按错误码写死的 `nextAction`
- `src/modes/native/read-*.ts`：读取去重前后都用 lstat 复核（dev/ino/size/mtimeNs/ctimeNs），且只认未经改写的渲染结果
- `AGENTS.md`：单文件 ≤1000 行、每条新行为一条断言、bench 永不裁决时间——工程纪律比功能更值得学

引用它的数据时请注意口径：官方自测"比 Windows 原装标准模式省 97%"是**自报**，没有第三方复现。

原项目 License 是 MIT AND Apache-2.0，其 Rust 工具基于 yc-duan 的 FastCtx（Apache-2.0）；本项目为独立实现的 MIT。

---

# ops-lite

把 dsh-ops（T-Auto/dsh-ops）省 token 的机制移植到 Hermes 的工具输出层。

dsh-ops 治的是 **DSH 里单次调用的工具层开销**：自带 shell、Rust 重写文件检索、批量执行和有界结果引用。
Hermes 这边的结构性问题不同（每轮重发整个历史，见 `hermes-token-cost-audit` 技能），
但「工具输出进上下文前先收敛、长结果留档、重复读取不重发」这三条是共通的。
本插件只做这三条，不碰 shell、不碰批处理（Hermes 已有 `execute_code` 与并行工具调用）。

## 它做什么

在 `transform_tool_result` 钩子上（结果进上下文之前、`post_tool_call` 之后）做三件事：

| 机制 | 行为 | 对应 dsh-ops |
|---|---|---|
| 有界收敛 | 超过预算的结果保留头尾，中段省略并留档；**信封感知**：`read_file` 的 `content`、`terminal` 的 `output`、`search_files` 的 `matches_text` 会被解码后按行收敛，再按原 JSON 结构重建 | file 组件的精简输出 + PTC 的有界结果引用 |
| 重复短路 | 同工具 + 同参数 + 同内容（sha1 一致）→ 只回「结果未变化」+ 全文路径 | file 组件的去重 |
| 错误预算 | 运行时标记为错误的结果用更大的预算（24k），错误信息是关键信号，别压太狠 | 减少报错循环 |
| 效果判定 | 失败的写操作附一行 `none / applied / unknown` + 该怎么走：标了 `unknown` 就别直接重试，先核验 | `Failure.effectState` |

外加一段 prompt-only 阶梯（系统提示里约 300 字）：先读头尾、按区间取回、别整篇重读、
一次能做完的独立事放进同一轮。**阶梯只做引导，不自动切换工具。**

## 为什么必须信封感知

Hermes 的大型工具结果几乎都是 JSON，换行被转义在字符串字段里：

```
{"content": "1|...\n2|...\n", "total_lines": 1500, "next_offset": 970}
```

直接按行切分的话，这样的结果「只有 1 行」，收敛等于什么都没省——第一版就是这么翻车的。
现在的流程是：解码大字段 → 按行保留头尾 → 把原信封结构重建，模型看到的还是它熟悉的 JSON。

## 配置

`config.yaml` → `plugins.entries.ops-lite`（改完即时生效，配置 5 秒缓存）：

| 键 | 默认 | 说明 |
|---|---|---|
| `max_chars` | 12000 | 超过这个字符数才收敛 |
| `head_chars` | 2600 | 保留的头部长度的字符预算 |
| `tail_chars` | 1400 | 尾部 |
| `error_max_chars` | 24000 | 错误结果的预算 |
| `repeat_min_chars` | 2000 | 多大的结果才参与重复短路 |
| `repeat_shortcircuit` | true | 关掉可全部禁用短路 |
| `spill_max_files` | 300 | 保留最新多少个留档文件 |
| `spill_ttl_hours` | 168 | 留档文件 TTL（0=不按时间清） |
| `effect_state` | true | 失败的写操作标注效果三态（关掉则完全不加字） |

留档目录：`%LOCALAPPDATA%\hermes\plugin-data\ops-lite\spill\`（按内容 sha1 命名，天然去重）。
账本：同目录 `savings.jsonl`。

## 行为与边界

**落盘**

- 只在「超预算」或「参与重复短路」时写留档；小结果落盘再回收没有意义。
- 留档逐字节保存原文（不转换换行），按内容 sha1 命名，天然去重。
- 回收按数量与 TTL 进行，每 25 次调用检查一次，短暂超出上限属正常。
- 回收同时清理 `.txt` 与 `.part`，失败留下的半成品不会堆积。

**收敛**

- 信封感知：先解码大字段、按行收敛，再按原结构重建，模型看到的还是熟悉的 JSON。
- BOM、紧凑 JSON 数组、多个中等大小的值对象都走文档分支；数组包成 `{"_ops_lite": …, "items": […]}`，元素不丢。
- 任何路径都不返回比原文更长的结果。
- 压缩有下限：键名、数组元素、标记本身都占位，小对象收敛收益有限。

**取回**

- 信封类结果另存一份字段专档 `<sha1>.field.txt`，提示直接指向它；按行读留档会读到转义 JSON。
- 源文件提示的 offset 按「原起始行 + 头部行数」算，直接落在被省略的中段。

**账本**

- 节省按实际返回长度计算，不虚报。
- 取回率自动统计自 2026-10-19 起，更早的约 19% 是人工反查，两者不要混看。
- 追加写入；进程内串行化，跨进程仍是尽力而为。

**预算**

- 全局 `max_chars`，按工具覆盖：`skill_view` 6000、`skills_list` 4000、`session_search` 6000。
- 错误结果走 `error_max_chars`（更大），不压太狠。

### 效果判定（1.5.0–1.5.2）

失败的写操作标注 `none / applied / unknown` 并给出下一步。只标失败、只标写类与命令类工具；
成功调用和只读工具不加任何字符。

| 状态 | 判据（Hermes 报错文案） | 结论 |
|---|---|---|
| `none` | Could not find a match / The file was NOT modified / permission denied / invalid argument | 未写入，修正后可重试 |
| `applied` | was modified but… / written but… / partially applied | 已变更，不要重试，先核验 |
| `unknown` | 其余写错误；命令工具的超时、被杀、broken pipe | 先核验再决定 |

JSON 信封里加 `_ops_effect` 键（保持可解析），纯文本追加一行；开关 `effect_state`。

账本每条判定带 `evidence`：命中的短语，或 `no-pattern: <工具原话>`。`analyze_ledger.py` 直接可见。

边界与限制：只有失败才标；命令工具只在被截断时标，退出码非零不标。
判定基于报错措辞，工具换文案时会退化为 `unknown`——保守方向，不会误判成 `none`。

并发与成本：账本追加串行化（8×60 并发全落）；200 KB 错误文本分类 < 2 ms。

**版本 1.5.2**。

## 收敛的结构性下限（别被数字误导）

收敛有下限——每个键/元素本身占位，标记本身也占位。实测：

| 形状 | 原 | 收敛后 |
|---|---|---|
| 400 元素数组（每个元素一个小对象） | 40,290 | 17,076 |
| 500 键对象（每值 300 字符） | 157,904 | 31,523 |
| 单键大文档（少数超长值） | 99,847 | 10,703 |
| read_file 信封（1200 行） | 79,329 | 3,641 |

键/元素越多，越收敛不到预算。此时**优先保住可解析性**（键与元素都不丢），不为数字好看去删结构。

## 命令与状态


```
/ops           近 24 小时省了多少
/ops 7d        任意小时/天：6h、30d、all
/ops session   只看当前会话
/ops files     最新留档文件
/ops clear     清空账本（留档文件按 TTL/数量回收）
```

启用/停用：`hermes plugins enable ops-lite` / `disable ops-lite`（桌面 App 与 gateway 会热重载钩子；
系统提示里的阶梯段要新会话才生效）。

## 不做什么（有意为之）

- 不改工具参数、不切工具、不替模型做决定（`tool_execution`/`tool_request` 中间件有这能力，但那是另一类风险）。
- 不动 `write_file` / `patch` / `todo_list` / `clarify` 的结果——这些是契约，不是内容。
- 不短路 `web_search` / `vision_analyze` 这类结果可能合理变化的工具，只对文件类读取去重。
- 不联网、不读凭据、只写自己的数据目录。

## 已知坑

- **`write_file` 会被自己的收敛挡住**：插件生效时，用 `read_file` 读一个超过预算的源文件，
  拿到的是收敛后的内容（这是设计使然）。此时去改同一个文件，Hermes 的写入守卫会因为
  「没读到完整内容」拒绝写入。改插件自身源码时先 `hermes plugins disable ops-lite`，
  或者把 `max_chars` 临时调大。
- 阈值调太小会打断正常工作流（读源码就是典型场景），默认 12000 是权衡后的取值；
  真正的肥肉是 20k~120k 字符的那些结果。
- **重复短路与上下文压缩的交叉**：若上一轮那份结果后来被上下文压缩掉了，模型这轮只会看到
  「结果未变化」而手里没有内容。标记里带了留档路径，按提示重读即可——这是有意的取舍
  （宁可让模型多读一次，也不重复回显一份膨胀的正文）。
- 重复短路的记忆在进程内（最多 200 条），重启即清空——这是有意的，避免跨会话拿到陈旧的「未变化」。
