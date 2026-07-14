# 状态与调用契约

本项目没有 HTTP API。对外契约是 CLI；服务间契约是 `state/<book-slug>/` 中的持久化状态、
SQLite 事务和 JSONL 事件。

## CLI

### 翻译或续跑

```bash
uv run trans-novel translate <input> [--chapter N] [--format epub|txt] [--polish|--no-polish] [--qa|--no-qa]
uv run trans-novel resume <input> [--format epub|txt] [--out PATH] [--polish|--no-polish] [--qa|--no-qa]
```

认证方式：由 `config.yaml` 的 `llm.api_key_env` 指定环境变量。仓库默认是
`LONGCAT_API_KEY`；密钥不得写入命令历史示例、配置或 state 产物。

返回方式：成功时写入/更新 state 并导出目标文件；LLM 术语抽取失败本身不会使命令失败。
命令会提示仍处于 glossary pending 的章数。翻译、SQLite、文件系统或 checkpoint
持久化失败会以异常和非零退出状态报告。

### 状态与工具

```bash
uv run trans-novel status <input>
uv run trans-novel tools glossary <input> list
uv run trans-novel tools glossary <input> conflicts
uv run trans-novel tools qa <input>
uv run trans-novel tools report <input>
uv run trans-novel tools assemble <input>
```

同一 run 不支持多个 writer 并发调用。只读命令也应避免与恢复、目录替换等运维操作并发。

`tools assemble` 只读取已有 state，不调用 LLM，也不改变 chapter target 或 glossary
checkpoint。EPUB 模板回填时：

- 只把显式 `title_translated` 投影到 NCX/NAV、章节 HTML `<title>` 和 EPUB2 guide；
- NCX/NAV 按相对目录文件解析后的完整 ZIP 路径与原标签匹配，不按 basename 匹配；
- 每条无 fragment 的旧状态标题规则只消费一次，未追踪的小节标签、层级、id 和 href 保持原样；
- `dc:language` / NCX `xml:lang` 写目标语言，全书 `dc:title` / NCX `docTitle` 保持源书名。

## 运行目录

```text
state/<book-slug>/
  manifest.json
  analysis.json
  context.json
  glossary.db
  events.jsonl
  report.json
  chapters/ch<N>.json
```

JSON 文件通过临时文件加 `os.replace` 原子替换。`events.jsonl` 是追加式审计记录，不是
完成状态数据库。`context.json` 也是缓存：翻译前会从此前 `status=done` 的 chapter JSON
重建，不能用它覆盖或恢复正文 target。

## Manifest 契约

`manifest.json.chapters[]` 的进度字段：

```json
{
  "state_format_version": 1,
  "analysis_glossary_status": "done",
  "glossary_generation_id": "<random generation id>",
  "titles_status": "done",
  "chapters": [
  {
  "index": 14,
  "title": "11 Volatility Spreads",
  "status": "done",
  "glossary_status": "pending"
  }
  ]
}
```

- `state_format_version`: 当前 checkpoint/identity 状态契约版本。
- `analysis_glossary_status`: `pending|done`，表示 Analyzer 初始术语 seed 是否已原子提交。
- `glossary_generation_id`: 当前 run 拥有的 `glossary.db` 世代；缺库或世代不匹配时失败关闭。
- `titles_status`: `pending|done`，表示标题是否已在最新一次术语恢复后成功生成；失败或中断会重试。
- `status`: `pending|done`，表示章节主流程。
- `glossary_status`: `pending|done`，表示 canonical batch、章级窗口以及派生章级父
  checkpoint 全部匹配；兼容旧状态时由全部 batch 与直接章级 v1 父记录共同证明。

新 run 从初始化起写入 `glossary_status=pending`。旧 manifest 没有该字段时不会全局重开
所有 `status=done` 章节，以免升级后对整本已完成书籍产生意外模型费用；仍处于翻译
pending 的旧章会进入运行，并在处理时显式写入 glossary pending/done 状态。
只有能正向证明没有 `state_format_version`、顶层新状态字段、章节 `glossary_status` 和 plan
的旧 manifest，第一次打开时才会绑定当时已经存在的数据库 generation。新格式 manifest
会同时写入 `state_format_version=1`；新格式 manifest 缺 generation 会失败关闭，不能借
legacy 迁移重新绑定。数据库缺失也不会自动创建空库；
manifest 缺失而目录仍有数据库或其他 state 产物时同样停止。新格式中显式标为 done 且带 version 1 plan 的章节，会在 prepare
时核验 batch 与 chapter checkpoint，缺失或指纹失配会自动改回 pending。
正向 legacy 绑定会给当时每章写 `glossary_legacy=true`；只有携带该迁移证据的 plan/status
双缺失才继续按 legacy 处理，章节一旦进入新状态就移除标记。pending+无 plan 是首次处理前
的合法新状态，空章 done+无 plan 也合法；没有 legacy 标记的双缺失、其他单标记、非法枚举
或 unsupported plan 组合均失败关闭。
`glossary_legacy` 若为非 true，或与 status/plan 同时存在，也属于矛盾状态并失败关闭。
`glossary_legacy` 键也会阻止 manifest 在顶层 version/generation 丢失后再次执行首次绑定。
这里的“无 plan”专指 `glossary_plan` 键不存在；显式 JSON `null` 仍是存在但非法的 plan，
在首次 adoption、pending、done 和 legacy-hole 恢复中都会失败关闭。

## Chapter glossary plan

`chapters/ch<N>.json.meta.glossary_plan`：

```json
{
  "version": 1,
  "units": [
    {
      "start_index": 0,
      "count": 4,
      "source_fingerprint": "<sha256>"
    }
  ]
}
```

`start_index` 是章内文本段下标，`count` 是连续段数，`source_fingerprint` 由该 unit 的
规范源文计算。version 1 计划持久化后不得在普通续跑中重排或重算。该 source-only 指纹
与 SQLite checkpoint 的 source/target 翻译指纹用途不同。

新窗口化章级抽取另在 `chapters/ch<N>.json.meta.chapter_glossary_plan` 中持久化 version 1
`chapter_window` 计划。它从上述 canonical units 确定性生成，约束如下：

```json
{
  "version": 1,
  "max_units": 3,
  "max_source_chars": 6000,
  "fingerprint": "<plan sha256>",
  "windows": [
    {
      "start_unit": 0,
      "unit_count": 3,
      "start_index": 0,
      "count": 12,
      "source_fingerprint": "<window source sha256>"
    }
  ]
}
```

- `start_unit`/`unit_count` 定位 canonical unit；`start_index`/`count` 定位章内连续文本段。
- 每个窗口最多含 3 个 canonical unit，按 `"\n"` 拼接后的源文最多 6000 字符。
- 存在后续窗口时，下一窗口从前一窗口最后一个 unit 开始，固定重叠 1 个 unit 并无缝覆盖
  整章。单个 unit 超限或无法保留重叠会使计划失败关闭。
- 窗口 `source_fingerprint` 绑定源段；顶层 `fingerprint` 绑定版本、两个上限和全部有序窗口字段。
- 已保存计划是续跑契约，不能按当前 pending 位置重新切窗。

旧 chapter JSON 没有窗口计划时，既有 `scope=chapter, version=1` 直接章级 checkpoint 仍可
继续与全部 batch checkpoint 共同验证；新增窗口计划的章节使用 version 1 `chapter_window`
子记录和 version 2 派生父记录。

## LLM glossary payload

模型响应外层：

```json
{
  "terms": [
    {
      "source": "original term",
      "target": "规范译名",
      "reading": "",
      "type": "术语",
      "gender": "",
      "aliases": ["source alias"],
      "note": ""
    }
  ]
}
```

`source` 和 `target` 必须是 trim 后非空字符串。可选标量只接受字符串；`type` 的默认值
是 `术语`，其他标量默认空字符串。`aliases` 必须是数组，且仅字符串元素有效。非规范
候选在 extractor 边界被跳过或回默认，不能直接传给 SQLite driver。aliases 会按首次出现
顺序去重。

空结果的成功载荷必须严格等于 `{"terms":[]}`。`[]`、`{}`、非数组 `terms`，以及带额外
根字段的空结果都属于协议失败，不能提交完成 checkpoint。术语请求按最终序列化后的 system
和 user messages 计量，必须不超过 30000 字符；源文/译文不截断，只能丢弃完整参考术语项。
响应 summary 会报告 `reference_terms_dropped`、`reference_chars` 和 `prompt_chars` 供审计。

该响应契约作用于模型的完整原始文本：去除首尾空白后必须能被一次 `json.loads` 完整解析，
顶层对象的 key 集合必须精确等于 `{"terms"}`。拒答、解释文字、Markdown 代码围栏，或在
任一此类文本中嵌入 JSON 都不会被抽取为成功结果。原始 JSON 解析失败记
`error_kind=model_or_json_error`；顶层多键记 `error_kind=response_extra_keys`。两者均不写
checkpoint，`glossary_status` 保持 pending。

## Translation payload

批量翻译响应必须是 `{"translations":["...", "..."]}`，数组长度与输入严格相等，且每项
必须是非空字符串。空项或非字符串项会使该批重试，最终按单段兜底；仍失败的段保持空 target
和 pending 状态，不得标成 done。润色 `polished` 数组中的空项/非字符串项逐段回退原译，
定向重译的非字符串项则拒绝采纳。

标题请求只渲染在全部扁平化标题源文中实际命中的术语：先读取全部术语，再按 source/alias
边界匹配得到 selected 集合。`titles` 数组只接受非空字符串；畸形项逐条回退扁平化后的源标题。
术语审计的
`unifications` 只能引用已侦测到的 source，并只能从已观察到的 current/variants 中选择
canonical 和替换变体。一致性机械修复的 `wrong`/`right` 必须都是字符串；任何容器、数字或
null 都会被丢弃。回译数组含非字符串时按对齐失败报告，不进入语义比对。

## SQLite checkpoint 契约

批量写接口的逻辑契约是：

```text
upsert_terms(terms, chapter=<N>, checkpoint=<unit>)
```

全部输入先预验证；glossary、`term_conflicts` 与对应 batch/window 子 checkpoint 在同一
SQLite 事务提交。batch 和兼容章级记录保存在 `glossary_extraction_checkpoints`，key 为
`(scope, chapter, start_index)`；窗口记录保存在独立表
`glossary_chapter_window_checkpoints`，主键为 `(chapter, start_index)`，并额外绑定非空
`plan_fingerprint`。匹配条件还包括 version、count、source/target 翻译 fingerprint 和当前
store generation ID。任何持久化异常都 rollback 并向上抛出。

窗口子记录固定为 `scope=chapter_window, version=1`。batch 和直接章级 v1 记录禁止携带 plan
fingerprint。派生父记录为 `scope=chapter, start_index=0, version=2`，`count` 是精确有序
batch partition 的段数总和，并绑定同一 plan fingerprint。

父记录通过 `BEGIN IMMEDIATE` 事务推导：store 在同一写事务内验证当前 generation/plan 下的
batch 和 window 记录与预期集合完全相等，再以 domain-separated SHA-256 绑定 generation、
计划和全部有序子身份，最后提交 v2 父记录。统一的 v2 完成检查仍会在一个读快照中同时重查
全部子记录和父记录；缺少预期记录、当前计划下存在额外记录或指纹漂移都返回未完成。其他
plan fingerprint 下的孤立窗口记录不会替代当前计划的预期记录。

普通单条 `upsert_term` 保持单条提交语义；需要 checkpoint 正确性的编排路径必须使用批量
事务，不能逐条 commit 后再单独写 checkpoint。

## 事件契约

所有事件至少含 `ts` 和 `event`。术语事件如下：

| 事件 | 条件 | 关键字段 |
| --- | --- | --- |
| `batch_glossary_extracted` | batch 术语与 checkpoint 原子提交成功 | `chapter`, `start_index`, `count`, `checkpoint_version=1`, `completed=true`, `fingerprint`, `generation_id`, `summary`；不含 `plan_fingerprint` |
| `chapter_glossary_extracted` | 保留的直接章级 v1 事件 emitter | `chapter`, `start_index=0`, `count`, `checkpoint_version=1`, `completed=true`, `fingerprint`, `generation_id`, `summary`；不含 `plan_fingerprint` |
| `chapter_glossary_window_extracted` | 窗口术语与 `chapter_window` 子 checkpoint 原子提交成功 | `chapter`, `start_index`, `count`, `checkpoint_version=1`, `plan_fingerprint`, `completed=true`, `fingerprint`, `generation_id`, `summary` |
| `chapter_glossary_window_skipped` | 窗口子 checkpoint 与当前 generation/plan/译文匹配 | `chapter`, `start_index`, `count`, `checkpoint_version=1`, `plan_fingerprint`, `fingerprint`, `reason=checkpoint_match` |
| `chapter_glossary_derived` | 精确 batch/window 子集合已推导并提交 v2 父记录 | `chapter`, `checkpoint_version=2`, `protocol_version=2`, `plan_fingerprint`, `fingerprint`, `batch_count`, `window_count`, `generation_id`, `completed=true` |
| `chapter_glossary_derivation_deferred` | 预期子集合尚不完整，v2 父记录未提交 | `chapter`, `plan_fingerprint`, `batch_count`, `window_count`, `completed=false` |
| `glossary_extraction_failed` | 模型调用或协议失败 | `phase=batch|chapter|chapter_window`, `chapter`, `start_index`, `count`, `fingerprint`, `error_kind` |
| `glossary_persistence_failed` | SQLite/checkpoint 提交失败 | `phase`, `chapter`, `start_index`, `count`, `fingerprint`, `summary`（如有）, `error` |
| `batch_glossary_skipped` | batch checkpoint 有效或 legacy 证据已晋升 | `chapter`, `start_index`, `count`, `fingerprint`, `reason`；晋升时另含 version/generation |
| `chapter_glossary_skipped` | 兼容 v1 或完整 v2 章级证明有效 | 公共字段 `chapter`, `fingerprint`, `checkpoint_version`, `reason=checkpoint_match`, `completed`；v2 另含 `protocol_version=2`, `plan_fingerprint` |
| `glossary_plan_created` | 首次持久化 canonical plan | `chapter`, `version`, `unit_count` |
| `glossary_plan_invalid` | plan 范围、版本或 source fingerprint 不合法 | `chapter`, `error`；随后停止续跑 |
| `chapter_glossary_plan_created` | 首次持久化确定性窗口计划 | `chapter`, `version`, `fingerprint`, `window_count`, `max_source_chars` |
| `chapter_glossary_plan_invalid` | 窗口约束无法满足或持久化计划与重建结果不等 | `chapter`, `error`；随后停止续跑 |
| `chapter_state_invalid` | `status=done` 但仍有空 target | `chapter`, `reason`；随后停止续跑 |
| `legacy_chapter_translation_reopened` | 无新 plan/status 的旧章 done 但有空 target | `chapter`, `missing_indices`, `reason`；只补空段 |
| `analysis_glossary_seed_failed` | Analyzer 结果已有，但初始术语持久化失败 | `phase`（恢复时）, `error`；随后抛出 |
| `analysis_glossary_seed_repaired` | 从已保存 analysis 补交 seed 成功 | `term_count` |
| `chapter_glossary_reconciled` | 已完成正文的章节执行纯术语恢复 | `chapter`, `completed` |
| `glossary_post_review_refreshed` | 自动修订 target 后刷新术语检查点 | `chapter`, `changed_indices`, `unit_starts`, `completed` |
| `chapter_glossary_reopened` | manifest done 与 SQLite checkpoint 失配 | `chapter`, `reason` |
| `chapter_glossary_state_invalid` | plan/status 标记缺失、非法或互相矛盾 | `chapter`, `reason`；随后停止续跑 |
| `glossary_checkpoint_invalidated` | 审计工具改写 target，章节重新排入术语恢复 | `chapter`, `reason` |
| `titles_invalidated` | 术语仍 pending，旧标题已清除并等待重建 | `reason` |
| `titles_translated` | 标题响应有效并写回 manifest | `reference_terms_total`, `reference_terms_selected`, `titles[]`；每项含 `index`, `source`, `target` |
| `autofix_skipped` | reviewer 命中续跑前已有 target，禁止自动覆盖 | `chapter`, `index`, `reason=saved_target_immutable` |

`summary` 包含 `inserted`、`updated`、`conflict`、`unchanged` 四个计数，还可包含
`received`、`accepted`、`rejected`、`normalized` 和 reference 规模统计。当前成功事件必须
明确 `completed=true`；它的全零术语计数仍由 SQLite checkpoint 证明有效。没有
checkpoint version 的 legacy 全零 summary 不构成完成证据。失败事件不得伪造成功 summary。
日志中的 error 应截断并避免包含 prompt、原文全文或认证信息。

`chapter_glossary_derived` 只表示当时成功写入父记录；普通恢复仍必须调用统一 v2 matcher
复核 exact children 与父记录，不能把事件本身当成完成证明。

## 兼容与恢复

进入兼容恢复的旧 run，其正文 target 是可复用的。旧 `batch_glossary_extracted` 只有在
summary 四个计数完整、总和大于零，并且相同 unit 的 `batch_translated` 可重建一致的
source/target 翻译指纹时才可晋升为 SQLite checkpoint。旧章级成功事件证据不足时重新
执行章级抽取。已完成且 manifest 缺 `glossary_status` 的旧章不会被升级过程自动重开。
例外是旧章同时缺 version 1 plan 且存在空 target：它会被受控改回正文/术语 pending，只补空段，
已有非空 target 不会再次送给翻译模型。新格式 done+empty 仍作为状态矛盾失败关闭。

完整备份/恢复流程见 [开发设计](dev.md#状态备份与恢复)。恢复另一份 `glossary.db` 后，
不能在不同 generation 之间移植 checkpoint。对带 version 1 plan 的新格式章节，编辑 JSONL
或把 manifest 强制改为 done 也会在下一次 prepare 的 SQLite checkpoint 审核中被重新打开；
无 plan/无 `glossary_status` 的 legacy 章节仍按上面的保守兼容策略处理。

已经存在的直接章级 `scope=chapter, version=1` checkpoint 不要求补造窗口子记录，并继续按
原整章翻译 fingerprint 验证。只有持久化了窗口计划的新章节采用 v2：所有预期 batch 和
window 子记录及其派生 parent 必须同时匹配才是 done。若最后一个 child 已提交但 parent 尚未
提交，续跑只执行事务内推导，不重复请求已匹配窗口。
