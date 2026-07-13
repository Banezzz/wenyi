# 变更记录

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
