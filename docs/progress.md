# 变更记录

## 2026-07-14

### EPUB2 系统目录层级安全回填

- 修复章节译名先写入后被 source-only TOC 项覆盖的问题，阅读器系统目录现在使用已保存的
  章节译名。
- NCX/NAV 从 basename 匹配改为规范化 ZIP 完整路径加原标签匹配；一条旧状态规则只消费
  一次，不再把同文件下全部 fragment 小节折叠成章节父标题。
- 同步更新 NCX 目标语言、章节与额外 TOC 文档的 HTML `<title>`、EPUB2 guide 标题；保留
  当前“全书书名不翻译”的 OPF/NCX 契约。
- 真实书离线重组验证：167 个 navPoint 的 id、层级和 href 全部不变，132 个嵌套标签与源书
  一致，唯一标签由错误输出的 35 个恢复到 160 个，35 个已追踪父项写入中文。

影响范围：EPUB 模板回填和目录/文档标题元数据；不调用 LLM、不重译正文、不修改 state。

验证方式：`uv run pytest tests/test_assemble.py`、全套 pytest、ZIP/NCX 结构审计、正文 body
逐文件比对和 `git diff --check`。

### 章级术语窗口与可证明完成态

- 把整章术语复核拆成持久化的 version 1 `chapter_glossary_plan`。计划记录
  `version/max_units/max_source_chars/fingerprint/windows`；每个 window 记录
  `start_unit/unit_count/start_index/count/source_fingerprint`。
- 每窗最多 3 个 canonical unit、源文不超过 6000 字符；后续窗口固定回退 1 个 unit 与前窗
  重叠，避免长章一次请求触发拒答或上下文失控。
- 按最终序列化 system/user messages 设置 30000 字符硬上限；源文和译文保持完整，预算不足
  时只丢弃整条参考术语，并在 summary 中报告实际 prompt/reference 规模。
- 空抽取仅接受严格 `{"terms":[]}` 作为成功证明；其他空形状继续保留 pending，不写完成
  checkpoint。
- 非空 `terms` 只要包含任一被拒候选，持久化路径就整体返回 `terms_rejected`；有效子集与
  checkpoint 均不写入，避免部分畸形响应误晋升为完成态。
- 术语抽取改为对模型完整原始响应执行严格 `json.loads`，且顶层只允许唯一 `terms` 键；
  拒答、解释文本、代码围栏及其中嵌入的 JSON 都会失败并保持 pending，不再被宽松解析器
  误晋升为完成 checkpoint。
- 新增独立 `glossary_chapter_window_checkpoints` 表。窗口术语、冲突记录与 child checkpoint
  在同一事务提交，任一步失败全部 rollback。
- 章级完成改为 v2 派生父记录：在 `BEGIN IMMEDIATE` 内核对精确有序 batch/window children，
  再以 generation、计划指纹和全部 child 身份推导并提交 parent。
- v2 `done` 会同时复核全部预期 children 和 parent；缺少、额外、指纹漂移或旧计划记录都会
  自动回到 pending。最后一个 child 后中断时只需补推导 parent，不重复已完成窗口。
- 保留旧 run 的全部 batch 加直接章级 `scope=chapter, version=1` 父 checkpoint 兼容路径，
  不强迫历史状态迁移或重做术语调用。
- 新增 `chapter_glossary_window_extracted`、`chapter_glossary_window_skipped` 和
  `chapter_glossary_derived` 事件，区分窗口提交、checkpoint 命中和父记录推导；子记录不足时
  另记 `chapter_glossary_derivation_deferred`。
- 标题翻译只注入标题源文实际命中的 source/alias 术语。真实书验证中，参考块从 10931 项、
  628233 字符缩减到 56 项、3487 字符，避免最终标题请求超出上下文；`titles_translated`
  新增 `reference_terms_total` 和 `reference_terms_selected` 记录筛选规模。

影响范围：glossary prompt/extractor/store、章级术语编排、chapter meta、SQLite schema、
恢复完成判定和事件日志。现有直接章级 v1 状态保持可读可续跑；窗口状态使用独立 child 表，
不会覆盖历史 checkpoint。

验证方式：

```bash
uv run pytest tests/test_glossary_agents.py tests/test_glossary.py tests/test_orchestrator.py
uv run pytest
uv run python -m py_compile trans_novel/glossary/extractor.py \
  trans_novel/glossary/store.py trans_novel/pipeline/orchestrator.py
git diff --check
```

## 2026-07-13

### 术语抽取类型与断点恢复加固

- 在 LLM JSON 边界验证 glossary 字段，拒绝 list/dict/null/number 流入 SQLite 文本列，
  并规范 `aliases` 的容器和元素类型。
- 把模型/协议失败降级为可续跑的术语 pending；SQLite 或 checkpoint 持久化错误仍为 fatal，
  防止把不可靠状态报告为成功。
- 引入版本化 canonical `glossary_plan`、SQLite 原子 checkpoint 和每章
  `glossary_status=pending|done`。
- Analyzer 初始术语 seed 增加独立 pending/done 状态，存储失败后可复用已保存 analysis 重试。
- Analyzer 结果改为先保存、再 seed、最后标 done，关闭 SQLite 已提交但分析响应未落盘的窗口。
- manifest 绑定 glossary generation；已有 run 缺库、换库或无 manifest 的孤儿产物一律失败关闭，
  `status` 不再创建空库。
- 新 run 增加 `state_format_version`；只有完全不含新格式标记的 legacy manifest 能首次绑定现存
  数据库，新格式单独丢 generation 不再被当作 legacy 重新授权。
- prepare 会核验显式 done 章节的 batch/chapter checkpoint，缺失或译文指纹漂移时重新调度术语恢复。
- legacy 判定收紧为 plan/status 两个标记同时缺失；单标记或 done+非空章无 plan 等矛盾状态
  失败关闭，不会静默排除在重试集合外。
- legacy 首次绑定会持久化逐章 `glossary_legacy` 迁移证据；新格式章即使同时删除 plan/status，
  也不能再次伪装成 legacy 绕过 checkpoint 对账。
- plan/status 缺失判定改为严格 key presence；显式 `glossary_plan: null` 在 adoption 与所有恢复
  状态中都视为非法，不再静默重建。
- 续跑按计划 unit 复用已有译文，只翻缺失 target 的连续段，并在命中术语 checkpoint 时
  跳过重复抽取。
- 已保存 target 的不可变约束扩展到章末 autofix；review 仍可报告问题，但不能把旧段再次送
  给译者或覆盖，只允许自动修订本轮新补段。
- 旧格式 done 章若遗留空 target，会受控重开并只补空段；新格式同类矛盾仍失败关闭。
- 翻译的等长响应若含空项会重试并逐段兜底；润色空项回退润色前译文，避免再次制造空 target。
- 翻译/润色/定向重译只接受字符串元素；`null`、对象、数组或数字不会被字符串化为伪译文。
- 标题、回译、术语审计和一致性机械替换同步收紧类型契约；术语审计还限制在已发现候选集合，
  防止模型凭空制造全局替换。
- 章末自动修订改变 target 时，受影响 unit 和章级 checkpoint 会按最终译文指纹刷新。
- 术语/一致性审计改写正文后会把相关章节和标题置 pending，续跑按新译文刷新 checkpoint。
- 回译抽样改为从最终译文确定性重建并映射章内 index，覆盖中断恢复与 autofix 后译文。
- 标题新增持久化 pending/done 状态；术语恢复后标题失败或中断不会被旧标题字段永久跳过，
  报告与 CLI 会显示 pending。
- `resume` 补齐 `--out`、`--polish/--no-polish`、`--qa/--no-qa` 参数转发。
- rolling context 改为从此前已完成章节 JSON 重建，`context.json` 仅作缓存，关闭跨文件
  写入崩溃窗和 autofix 后旧译文残留。
- 扩充术语成功、失败和 checkpoint skip 事件，支持按 generation、fingerprint 和 unit 对账。
- README 切换为仓库当前的 LongCat / `LONGCAT_API_KEY` 默认配置，并补充状态备份与恢复入口。

影响范围：glossary extractor/store、章节编排、run manifest、事件日志和续跑行为。现有章节
JSON 仍可读取；进入兼容恢复的旧章，其事件仅在满足严格证据条件时晋升，否则保守重抽
术语，不重翻正文。

验证方式：

```bash
uv run pytest tests/test_glossary_agents.py tests/test_glossary.py tests/test_orchestrator.py
uv run pytest
uv run python -m py_compile trans_novel/glossary/extractor.py \
  trans_novel/glossary/store.py trans_novel/pipeline/orchestrator.py \
  trans_novel/pipeline/runstore.py
git diff --check
```

重点回归场景包括畸形 LLM 字段、术语事务 rollback、模型失败后主翻译完成、持久化失败向上
抛出、批次中点中断后只翻缺失段，以及 legacy checkpoint 的严格晋升/重抽。
