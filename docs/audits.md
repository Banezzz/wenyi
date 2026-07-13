# 未解决审计项

## Front-matter 标题子串误判

- 严重性：mid
- 影响范围：样章选择和全书理解预扫；正文翻译本身仍会执行。
- 触发条件：`_looks_front_matter` 当前对模式做任意子串匹配。例如 `Stock Index` 命中
  `index`，`Covered Calls` 命中 `cover`，会把正常正文章误当作前置材料。
- 复现方式：分别调用 `_looks_front_matter("Stock Index", <正文>)` 和
  `_looks_front_matter("Covered Calls", <正文>)`，当前均返回 true。
- 临时缓解：把受影响章节视为“缺少预扫摘要”，不要删除或重翻已有译文。质量敏感任务可在
  独立备份 run 中修正标题后重新生成分析/预扫。长期修复应使用词边界/精确标题规则，并补
  上述两个回归用例。

## 同一 run 不支持多 writer

- 严重性：high
- 影响范围：同一 `state/<book-slug>/` 的 manifest、章节 JSON、context、events 和 glossary。
- 触发条件：两个 `translate`/`resume` 进程同时写同一 run，或在 writer 运行时恢复/替换目录。
- 复现方式：并发启动两个指向同一输入和 `state_dir` 的 writer；SQLite 有 WAL 和 busy timeout，
  但 JSON read-modify-write、JSONL append 与跨文件状态没有进程级总锁，可能发生覆盖或状态漂移。
- 临时缓解：严格 single-writer；启动前用进程管理器或外部锁串行化，同一 run 备份/恢复前
  停止所有 writer。长期修复应增加 run-scoped 文件锁和明确的只读并发策略。

## Legacy checkpoint 只能保守晋升

- 严重性：low
- 影响范围：升级前已存在的长书 state；进入恢复的章节不会重翻已有 target，但可能重复
  调用术语模型；已完成且无 `glossary_status` 的旧章不会自动重新对账。
- 触发条件：旧 `batch_glossary_extracted` 缺完整正计数 summary、没有可匹配的
  `batch_translated` source/target 翻译指纹，只有旧 `chapter_glossary_extracted` 事件，
  或旧章已经 `status=done` 但 manifest 没有 `glossary_status`。
- 复现方式：从旧 run 移除/置零 success summary 或缺少配对翻译事件后续跑；系统无法证明
  对应 SQLite 效果完整，只能重新抽取。
- 临时缓解：升级前按开发文档创建完整 state 快照；对仍在恢复的章节接受一次保守重抽并
  保留 JSONL 对账。不要手工把 `glossary_status` 改为 `done`。长期修复可提供离线 legacy
  检查、选择性重开和迁移命令。

## 术语抽取没有 unit 级删除来源

- 严重性：low
- 影响范围：章末自动修订或术语审计改写译文后，对应 unit 会按最终译文重新抽取，但 glossary
  是跨章累积表，旧抽取项没有 unit provenance 可用于精确撤销。
- 触发条件：最终译文不再支持旧术语，或同一 source 在重抽后得到不同 target。
- 复现方式：先让一个 unit 抽取术语，再通过 review/autofix 改写相关 target 并重抽；旧 source
  可能仍保留，不同 target 会进入 `term_conflicts`，不会被盲目覆盖或删除。
- 临时缓解：以 checkpoint 证明“最终译文已完成重抽”，不要把它解释为术语表与单个 unit 的
  完全镜像；通过 `tools glossary conflicts`、QA 和人工锁定裁决差异。长期修复需增加 unit-term
  provenance，再设计不破坏跨章共享术语的引用计数/删除语义。
