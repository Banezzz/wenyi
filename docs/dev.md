# 开发设计

Python interpreter: `./.venv/bin/python`（日常命令仍统一使用 `uv run`）。

## 架构与模块边界

翻译流水线以 `trans_novel/pipeline/orchestrator.py` 为入口，主要边界如下：

- `ingest/` 负责把 EPUB、FB2、TXT 转成稳定的章节和段落序列。
- `agents/` 负责分析、翻译、润色、审校和提示词，不直接拥有持久化事务。
- `glossary/extractor.py` 是不可信 LLM JSON 到内部术语对象的验证边界。
- `glossary/store.py` 拥有 SQLite 术语、冲突、翻译记忆和术语 checkpoint 事务。
- `pipeline/runstore.py` 持久化 manifest、章节 JSON、上下文和追加式事件日志。
- `assemble/` 只消费已持久化结果并导出 EPUB/TXT，不决定翻译或术语进度。

章节 JSON 中的 `target` 是译文进度的权威来源；SQLite checkpoint 是术语处理进度的
权威来源。`manifest.json` 中的状态用于快速调度，`events.jsonl` 用于审计和受约束的
旧状态晋升，二者都不能替代 SQLite checkpoint 的正确性判断。

## LLM 术语字段验证

模型返回的 JSON 必须在进入 `GlossaryStore` 前收敛为明确类型：

| 字段 | 输入契约 | 非规范值处理 |
| --- | --- | --- |
| `source`、`target` | trim 后非空字符串 | list、dict、null、number 或空白字符串使整条候选被跳过 |
| `reading`、`gender`、`note` | 字符串 | 非字符串回退为空字符串 |
| `type` | 非空字符串 | 非字符串或空白值回退为 `术语` |
| `aliases` | 字符串数组 | 非数组回退为 `[]`；数组中仅保留 trim 后非空的字符串，并按首次出现顺序去重 |

验证层不把 list/dict 转成 Python repr，也不把单个字符串按字符拆成 aliases。字段清洗后
仍使用类型化术语对象传递，SQLite 层再做一次防御性预验证，避免未来其他调用方绕开
extractor 时重新引入不可绑定值。

## 错误边界

术语抽取是质量增强路径，译文持久化是主路径。两类错误必须区别处理：

- 模型调用失败、JSON 协议不符或候选字段不合法属于 `model/protocol` 错误。单条坏候选
  被丢弃；整次抽取不可用时记录 `glossary_extraction_failed`，翻译继续并保留
  `glossary_status=pending`，供续跑补齐。
- SQLite、磁盘、事务提交或内部持久化验证失败属于 `persistence` 错误。记录
  `glossary_persistence_failed` 后继续抛出，进程必须失败退出。吞掉这类错误会让内存、
  glossary 和 checkpoint 对进度产生互相矛盾的判断。

因此，“模型输出坏了”不能阻断已译正文，“无法可靠提交状态”也不能伪装成成功。

## 规范术语计划

每章第一次进入翻译时，根据稳定的源段落序列生成并持久化版本化计划：

```json
{
  "glossary_plan": {
    "version": 1,
    "units": [
      {
        "start_index": 0,
        "count": 4,
        "source_fingerprint": "<sha256>"
      }
    ]
  }
}
```

计划保存于 `chapter.meta.glossary_plan`。每个 unit 由起始段号、段数和源文指纹唯一描述。
计划一旦写入就作为该章的 canonical plan；续跑不得根据“当前还缺哪些 target”重新分批，
否则 checkpoint key 会随中断位置漂移。需要变更分批算法时必须提升 plan version，并提供
显式迁移策略。

## SQLite 原子 checkpoint

单个术语 unit 的提交顺序为：

1. 在内存中验证全部术语、chapter 参数和 checkpoint 字段。
2. 开启一个 SQLite 事务。
3. 在同一事务中写 glossary、可能产生的 `term_conflicts` 和对应 checkpoint。
4. 任一步失败则 rollback；成功后一次 commit。

事务边界保证不会出现“术语写了一半但 checkpoint 已完成”或“冲突记录残留但主写入失败”。
checkpoint 以 `(scope, chapter, start_index)` 定位，并匹配 `version`、`count`、
完整 source/target 对的 `fingerprint` 及当前 glossary store 的 `generation_id`。plan 的
`source_fingerprint` 只证明源分段没有变化；SQLite fingerprint 还证明术语抽取所依据的译文
没有变化。不同 generation 之间移植的 checkpoint 不会被误当成当前数据库的完成证明。
manifest 同时持久化 `glossary_generation_id`。已有 run 必须以只读现存模式打开数据库；
文件缺失、generation metadata 缺失或世代不一致都会停止，不能由 SQLite 隐式新建空文件。
若 manifest 缺失但同 slug 目录仍有数据库或其他产物，也会视为来源不明的孤儿状态并停止。
新 manifest 另写 `state_format_version=1`。generation 缺失时，只有顶层新字段、章节术语状态
和 glossary plan 全部不存在的 manifest 才能正向识别为 legacy 并绑定现存数据库一次；
绑定时同步写入 format version，并为每章记录 `glossary_legacy=true` 的迁移证据。章节首次
进入新术语状态时移除该标记。任何带新格式痕迹的 manifest 丢失 generation 都在打开数据库
前失败关闭；`glossary_legacy` 键本身也是新格式痕迹，不能用于第二次首次绑定。

章节 JSON 与 SQLite 无法组成一个跨文件原子事务，因此顺序固定为先原子保存译文 JSON，
再提交术语事务。若两步之间中断，续跑复用译文并只补术语。SQLite checkpoint 成功后，
manifest 才可把该章投影为 `glossary_status=done`。

## 续跑状态机

`manifest.json` 中每章维护两个正交维度：

- `status=pending|done`：正文、审校等章节主流程是否完成。
- `glossary_status=pending|done`：canonical plan 的 batch unit 和章级兜底是否均有有效
  SQLite checkpoint。

manifest 顶层另有 `analysis_glossary_status=pending|done`。Analyzer JSON 会先保存；只有初始
术语 seed 原子提交成功（或分析为空）才标记 done。若 SQLite 暂时失败，下次 prepare 直接
复用已保存 analysis 重试 seed，不再请求 Analyzer，也不会永久漏掉角色/初始术语。
`titles_status=pending|done` 把术语恢复与标题翻译串成持久化状态机：只要本轮开始时仍有术语
pending，系统先清除旧标题并把状态持久化为 pending；术语全部完成且标题响应有效后才标
done。标题调用失败或在两步之间中断，下次仍会重试，导出期间则回退源标题。

新格式若 `status=done` 但仍有空 target，会失败关闭。旧格式同时缺少 `glossary_status` 和
version 1 plan 时，done+empty 是历史流程曾接受空翻译的已知状态：prepare 会把该章受控
重开，只把空 target 连续区间送回译者，已有非空译文逐字保留。

调度集合是翻译 pending 与术语 pending 章节的并集。对每个 canonical unit：

1. 已有 target 的段落原样复用并重建滚动上下文。
2. 只把缺失 target 的连续 run 发送给翻译模型，避免重翻同 unit 中已完成的段落。
3. unit 译文完整后检查 SQLite checkpoint；匹配则跳过术语 LLM，缺失或失配则抽取并提交。
4. 所有 batch checkpoint 完成后运行章级兜底；章级 checkpoint 完成后将
   `glossary_status` 更新为 `done`。

“已有 target 原样复用”覆盖整次恢复流程，而不只覆盖批量翻译：章节载入时记录所有非空
target 的索引，章末 reviewer 可以报告这些段的问题，但 autofix 会记录 `autofix_skipped`
并拒绝把它们再次送给译者。只有本轮从空 target 新补出的段允许自动重译。

若章末 review 的自动修订改变了 target，系统会先保存新译文，再用新指纹重新抽取受影响的
canonical unit 和章级结果并刷新 checkpoint。`glossary_status=done` 因而证明最终落盘译文
已经完成这一轮抽取；术语库是跨章累积集合，旧派生项不会按单个 unit 盲删，译法差异仍走
`term_conflicts` 和后续 QA/人工裁决。

每次 prepare 还会审核带 version 1 plan 且显式 `glossary_status=done` 的章节。任一 batch 或
chapter checkpoint 缺失、世代不符或 target 指纹漂移，就把该章重新标成 pending。术语审计
工具改写正文时也会立即置 pending，因此普通续跑会补齐新的 checkpoint，而不重翻正文。
只有 plan/status 都不存在且章节带 `glossary_legacy=true` 时才继续归类为 legacy。新章的
pending+无 plan 表示尚未首次处理，空章的 done+无 plan 也合法；除此之外，没有迁移证据的
双缺失、只有一个标记、非法 status、unsupported plan 或非空章 done+无 plan 都是状态矛盾
并失败关闭。
legacy marker 必须严格为 true 且不能与 status/plan 共存；迁移到任何新状态时必须同一次
manifest 原子写移除 marker。
缺失按 JSON key presence 判定：`glossary_plan: null` 不是“无 plan”，而是非法持久化值，
不能触发 plan 重建或 legacy adoption。

回译抽样不依赖批次进程内临时列表。系统在 review/autofix 之后，从最终 source 按章号和
段号计算稳定样本；中断续跑会重建同一集合，模型返回的 sample-local index 再映射为章内 index。

翻译和润色响应也在 Agent 边界验证元素类型。`translations` 的任一非字符串或空字符串会让
整批进入重试/逐段兜底；`polished` 的空项或非字符串项只回退该段润色前译文；定向重译的
非字符串项直接拒绝采纳。不能用 `str(null)` 或 `str(object)` 伪造可持久化译文。

旧事件只能在严格条件下晋升为 checkpoint：同 key 的 `batch_translated` 能重建匹配的
source/target 翻译指纹，且旧 `batch_glossary_extracted.summary` 含四个完整计数字段并且
总数大于零。零计数、字段缺失或指纹不匹配时重新抽取。JSONL 只提供晋升证据，晋升后
仍以 SQLite 记录为准。

## 事件与可观测性

成功事件沿用 `batch_glossary_extracted` 和 `chapter_glossary_extracted`，并携带
`checkpoint_version=1`、`completed=true`、`fingerprint`、`generation_id` 和 `summary`。
当前版本的成功事件即使术语计数全零也可信，因为 checkpoint 与空结果在一个事务中提交；
只有没有 checkpoint 字段的 legacy 全零事件不能作为完成证据。

其他术语事件：

- `glossary_extraction_failed`：模型或协议失败；含 `phase=batch|chapter`、unit key、
  fingerprint 和 `error_kind`，主翻译可以继续。
- `glossary_persistence_failed`：SQLite 或 checkpoint 提交失败；记录后向上抛出。
- `batch_glossary_skipped`、`chapter_glossary_skipped`：命中有效 checkpoint，未重复调用模型。
- `glossary_plan_created`：首次持久化 canonical plan。
- `glossary_plan_invalid`：已保存计划与当前源段不一致；为保护译文而停止续跑。
- `chapter_state_invalid`：manifest 标记正文 done，但章节仍含空 target；停止而不猜测修复。
- `legacy_chapter_translation_reopened`：旧格式 done 章存在空 target；只重开缺口并保留已有译文。
- `analysis_glossary_seed_repaired`：从已保存 analysis 成功补交初始术语 seed。
- `chapter_glossary_reconciled`：正文早已 done 的章节完成一次纯术语恢复，含最终
  `completed` 状态。
- `chapter_glossary_reopened`：prepare 审核发现 done 投影与 SQLite checkpoint 不一致。
- `chapter_glossary_state_invalid`：plan/status 组合矛盾，不能安全归类为 legacy 或新状态。
- `glossary_checkpoint_invalidated`：术语/一致性审计改写 target，章节已重新置 pending。
- `titles_invalidated`：发现术语 pending 后清除旧标题，等待按最新术语重新生成。
- `autofix_skipped`：review 命中续跑前已有 target，但该段按不可变约束禁止自动重译。
- `glossary_post_review_refreshed`：章末自动修订改变 target 后，受影响 unit 与章级
  checkpoint 已按最终译文重新对账。

事件字段用于复现和对账，不作为正常运行中的完成判定捷径。
纯术语恢复不会把旧 target 再次写入 rolling context，避免较早章节的恢复任务污染后续
真正待译章节的上下文顺序。

`context.json` 是可重建缓存，不是 target 权威来源。每个待处理章节开始前，编排器按
manifest 顺序从此前 `status=done` 的章节 JSON 重建最近译文；章节结束后再从最终落盘
target 更新缓存。因此 status/context 跨文件写入间崩溃、乱序 `--chapter` 和章末 autofix
都不会把旧译文永久带入下一章。

## 状态备份与恢复

整个 run 包含 JSON 文件、JSONL 和 SQLite，必须作为一个一致性单元备份。即使 SQLite
backup API 支持在线复制，其他文件也没有跨文件快照，因此以下 runbook 要求先停止该
`state/<slug>/` 的所有 writer。

### 创建冷备份

1. 停止并确认没有针对该 run 的 `translate`、`resume` 或 glossary 写进程。
2. 设置路径并复制文件系统内容；随后用 SQLite backup API 重建快照中的数据库。

```bash
RUN='state/<book-slug>'
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT='../wenyi-state-backups'
SNAPSHOT="$BACKUP_ROOT/$(basename "$RUN")-$STAMP"
test -f "$RUN/manifest.json" && test -f "$RUN/glossary.db"
mkdir -p "$BACKUP_ROOT"
mkdir "$SNAPSHOT"
cp -a -- "$RUN"/. "$SNAPSHOT"/
rm -f -- "$SNAPSHOT/glossary.db" "$SNAPSHOT/glossary.db-wal" "$SNAPSHOT/glossary.db-shm"

SRC_DB="$RUN/glossary.db" DST_DB="$SNAPSHOT/glossary.db" uv run python - <<'PY'
import os
import sqlite3

with sqlite3.connect(os.environ["SRC_DB"]) as source:
    with sqlite3.connect(os.environ["DST_DB"]) as target:
        source.backup(target)
PY
```

3. 校验 JSON、SQLite 完整性并生成文件哈希。

```bash
SNAPSHOT="$SNAPSHOT" uv run python - <<'PY'
import glob
import json
import os
import sqlite3

root = os.environ["SNAPSHOT"]
for path in glob.glob(os.path.join(root, "*.json")) + glob.glob(
    os.path.join(root, "chapters", "*.json")
):
    with open(path, encoding="utf-8") as handle:
        json.load(handle)
with sqlite3.connect(os.path.join(root, "glossary.db")) as db:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"SQLite integrity_check failed: {result}")
print("snapshot structure: ok")
PY

(cd "$SNAPSHOT" && find . -type f -print0 | sort -z | xargs -0 sha256sum) \
  > "${SNAPSHOT}.sha256"
```

### 恢复

1. 停止 writer，并先为当前 run 留一个回滚副本。
2. 在快照目录中执行 `sha256sum -c`，确认原备份未变化。
3. 恢复到原路径，再次运行 JSON 和 `PRAGMA integrity_check` 校验。

```bash
RUN='state/<book-slug>'
SNAPSHOT='../wenyi-state-backups/<book-slug>-<timestamp>'
STAMP="$(date +%Y%m%d-%H%M%S)"

(cd "$SNAPSHOT" && sha256sum -c "../$(basename "$SNAPSHOT").sha256")
mv -- "$RUN" "${RUN}.before-restore-$STAMP"
cp -a -- "$SNAPSHOT" "$RUN"
```

恢复后把上一节校验命令中的 `SNAPSHOT` 指向 `$RUN`，再次解析 JSON 并运行
`PRAGMA integrity_check`；然后运行 `uv run trans-novel status <original-book>` 检查
manifest，再执行 `resume`。
不要把单独的 `glossary.db` 与另一时刻的章节 JSON 混合恢复；generation 和 fingerprint
会拒绝一部分不匹配 checkpoint，但不能把这种混合状态变成受支持的一致快照。

## 路线图

- 修正 front-matter 标题识别为词边界或精确规则，避免正文标题子串误判。
- 为同一 run 增加跨进程 writer lock；在此之前保持 single-writer 运维约束。
- 为 legacy checkpoint 提供显式检查/迁移工具，减少保守重抽带来的额外模型调用。
