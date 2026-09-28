# Draft：Affector 条件新鲜度与 GALHCG 分层知识图

状态：draft — 首轮端到端探索与源码核对完成；Affector 语义、图谱层级和后续施工边界待裁定

> 本文记录从确认 MCP 项目身份、建立项目图、沿图检索 Affector 性能需求，到发现图谱和 Agent 操作流程改进项的完整探索。本文是讨论与事实路由，不代表批准修改 Affector 或 GALHCG 服务。

## 目标与最终方向

探索一个需求：Affector 的部分内容应在依赖未变化时保持已知结果，避免不必要的高频 Tick 条件判断。探索同时检视 GALHCG 能否提供从系统概念、管理机制、底层机制到源码与测试的可追溯路径。

期望形成两类后续方向：

1. 在核实当前 Affector 条件依赖与 Tick 行为后，明确哪些计算可由事件失效、缓存或其他固化方式替代，哪些仍需保留逐 Tick 求值。
2. 让 GALHCG 表达有层级的概念分解、跨层关系、文件依据和可遍历路径，使未来的需求能从高层概念逐步缩小到可验证实现。

“固化”暂作为待定义词使用。它可能指运行期缓存条件结果、缓存有效 Entry 集、物化持续贡献，也可能被误解为持久化存档状态；本 Draft 不替用户裁定这些含义。

## 本次探索过程

| 阶段 | 实际行为与结果 | 暴露的问题 |
| --- | --- | --- |
| 确认项目身份 | 首次 MCP `status` 只返回 `default` → GALHCG；核对服务配置后再次查询，返回 `default` → GALHCG 与 `acprogram` → ACProgram。后续操作均显式传 `project_id`。 | 初次状态与后续状态不一致，变化原因未查明。Agent 必须以工具当前返回的根目录为准，出现冲突时重查并报告，不能按默认 ID 猜项目。 |
| 刷新清单 | GALHCG 刷新成功：23 文件、4 目录、335,998 元数据字节、扫描 4 ms。ACProgram 刷新成功：1,179 文件、114 目录、11,268,593 元数据字节、扫描 264 ms。 | 刷新结果需要保留项目身份、范围、状态、文件/目录数、字节量和耗时，不能只说“索引好了”。 |
| 构建试验图 | GALHCG 写入 5 个语义节点、13 条边；ACProgram 写入 9 个语义节点、50 条边。两次均按项目分别写入，并用 `search` / `context` 验证。 | ACProgram 图有 Project→Module→Concept/File，但三个 Concept 处于同一语义层，缺少概念内部的上下位关系。50 条边连接了大量文件，却仍不能表达管理机制如何分解为可单独检索的机制概念。 |
| 沿需求检索 | 在 ACProgram 图中搜索 `Affector`，得到 18 个文件名/路径命中；搜索 `Tick` 得到 Tick 文档、系统和测试。再以 `source` 搜索当前 `docs-829`、`src/engine`，最后预览 Affector Engine、ConditionDepIndex、TickSystem 和当前机制文档。 | `map` 搜索结果包含历史 `docs-828` 文件，也包含一般文件节点；它不等于 Affector 语义图，也不等于源码正文搜索。宽泛源码搜索可截断，需按目录和主题收窄。 |

## 当前事实与待核对问题

| 问题 | 已核实依据 | 对方案/验收的影响 | 结论状态 |
| --- | --- | --- | --- |
| Affector 是否每 Tick 全量重估？ | `src/engine/effect/affector-engine.ts` 的事件依赖索引会按命中事件对实例定向 `recheck`；`pollingInstances` 收纳 stat/未知依赖的保守轮询实例；`applyActiveEffects()` 每 Tick 重查该集合，并只对 Active Entry 执行显式 `perTickEffects`。当前正文见第 525–552 行附近。 | 需求应先量化剩余轮询范围，避免把已存在的事件驱动优化重复设计成新机制。 | 已核实；需继续对照完整调用链和测试 |
| 哪些依赖进入事件索引？ | `src/engine/expression/condition-deps.ts` 对 resource、affectionLevel、spotLevel、manager、flag、enhancement、tag、Extra 等依赖登记事件；stat/未知 target 会返回宽依赖。stat 也会对列举的宽事件失效，并保留 Tick 轮询。 | 需要决定要新增精确事件、将哪些结果缓存，或保留哪些无专属事件条件为轮询回退。 | 已核实；目标条件集合待裁定 |
| Tick 是否仅用于 Affector 条件判断？ | `src/engine/system/tick-system.ts` 每秒结算资源产出并发出 Tick；Affector 的显式 `perTickEffects` 与持续 flow 是独立行为。 | 条件重估、持续产出、逐 Tick 效果、资源结算应分别计量，验收不能用总 Tick 耗时替代细项。 | 已核实 |
| Affector 主体状态如何向消费者传播？ | `docs/docs-829/04-mechanisms/engine/effect-trigger.md` 说明 Latent/Active/Removed 生命周期；active flows、zoneModifiers、区域连接和服务能力是派生贡献，并在状态或 Entry 集变化时通知消费者。 | “固化内容”可能触及 AffectorEngine、GameNum/派生索引、事件失效和查询端，不可只改条件求值函数。 | 文档已核实；具体受影响函数与测试矩阵待核对 |
| 当前源码注释是否一致？ | `affector-engine.ts` 文件头称每 Tick 全量重估；同文件的依赖注册与 `applyActiveEffects()` 实现、当前机制文档均描述事件定向重估加局部轮询。 | 方案依据必须采用方法实现和当前机制文档，并处理陈旧注释；暂不据文件头概括当前行为。 | 已发现冲突；根因与注释修复待核对 |
| 现有测试覆盖在哪里？ | 搜索命中 `tests/engine/affector-engine.test.ts`、`affector-reconcile.test.ts`、`event-driven-reactor.test.ts`、`tag-stats.test.ts`、`tick-system.test.ts` 等。 | 后续需从目标条件和依赖失效路径选读具体断言；本次没有运行测试。 | 路径已定位；断言覆盖待核对 |

当前实施还存在一个性能历史记录 `task-0034-affector-performance-review`。该文件是归档快照，不作为现状依据；其中关于 ZoneModifier、flow 来源索引、事件合并和依赖索引成本的结论，必须逐项回到当前源码与测试重新检查，不能直接登记成当前未完成项。

## GALHCG 图谱结构核对

本次查看的 GALHCG 服务源码为 `src/project_preview/semantic_map.py`、`src/project_preview/server.py` 与 `README.md`。现有语义节点类型是 `Project`、`Module`、`Concept`、`File`；关系类型为 `contains`、`maps_to`、`depends_on`、`related_to`。`contains` 可表达 Project/Module 到 Module/Concept/File 的结构，但当前验证器不允许 Concept→Concept 的上下位结构。`context` 给出有限结构和一跳邻居；`map` 搜索按节点名、别名、摘要及文件信息找候选。

本次建出的 ACProgram 图让 `module:engine` 指向 Engine 模块卡和 `src/engine/index.ts`；Affector 子系统只在普通文件索引中出现。`search(map, "Affector")` 命中了文件名、路径和历史文档，没有一个表达“Affector 条件新鲜度”的语义节点。因此 Agent 需要人工从文件名搜索切换到 `source` 搜索与 `preview`，才能确认条件失效行为。

建议讨论的概念分层样例：

```text
Engine 持续规则系统
└─ Affector 生命周期与条件新鲜度（管理概念）
   ├─ 事件依赖登记与定向失效
   ├─ stat / 未知依赖的轮询回退
   ├─ Active Entry 与一次性激活效果
   ├─ 显式 perTickEffects
   └─ flow / zoneModifier 等派生贡献
        ↔ 事件、Mutation、GameNum 和 UI 查询关系
        → 实现文件、机制文档与测试文件
```

此结构只是样例，不是已裁定的节点命名或 schema。目标是表达：上位概念可拆到管理概念，管理概念再拆到机制概念；概念层之间可有方向明确的因果、失效、消费或依赖链接；每个概念可以链接源码、文档和验证文件。结构边与横向语义边应分开，避免把 `contains` 当作所有关系的通用含义。

## 本次行为暴露的改进项

### 图谱模型与查询

- **支持 Concept 层级。** 评估新增概念父子关系或允许 `contains` 扩展到 Concept→Concept；验证无环、多父节点策略、祖先/后代查询和删除行为。也评估以 `level` 字段标记 domain / management / mechanism，避免把深度写死为三层。
- **区分结构和横向关系。** 评估失效于、触发、派生、消费、实现等有向关系类型；`related_to` 可保留作弱关系，不代替可验证的因果语义。
- **让路径查询穿过多层。** 扩展 `context` 或新增路径查询，支持指定深度、祖先、子孙和横向邻居；结果应显示关系类型与方向，并限制规模、截断和分页。
- **让概念绑定“可验证文件”。** 概念需能关联源码、当前机制文档和测试；可探索 File 节点角色或 evidence kind，区分“实现”“解释”“验证”，避免只靠文件名匹配。
- **补充文件正文检索与语义路由。** 当前 `map` 文件命中不能代替代码语义搜索；需让 Agent 从顶层概念沿图到管理概念、机制概念和文件，并能解释命中理由。
- **为大项目维持概念覆盖边界。** 不追求给每个源码文件造概念节点；按稳定职责分组，控制节点摘要、层级和关系密度，并能标示未建图区域。

### Agent / MCP 操作流程

- `status` 必须先确认 MCP 项目 ID 与根目录。首次状态与后续状态冲突时重查；不能根据 `default` 猜项目，也不能把两个项目的文件或节点混用。
- 明确区分 `search(map)`、`search(source)`、`list_files`、`preview` 与 `context` 的证据能力。宽查询出现截断时缩小目录；历史目录按 `AGENTS.md` 标记为历史来源，不用作现状结论。
- 写边前从 `list_files` 按路径解析 File 节点 ID，避免手工抄错 ID；从 `preview` 取得确切版本令牌，并尽量在提交前立即刷新所有证据令牌。
- 写入前计算整个 batch 的文件依据条目数。当前 `update_map` 单次最多校验 32 条依据，即使节点数和边数尚未达到上限也会拒绝超限批次。
- 保留工具更新的原子性，但减少“修一个错误再重试”的轮次。可评估批次预检/preview、路径直连端点、一次返回全部可判定错误和精确说明证据预算。
- 更新后用语义搜索验证概念可发现，用 `context` 验证项目归属、层级、方向、文件依据和相邻关系；必要时再次检查另一项目，证明没有串图。
- 在输出中分别报告工具实测、源码事实、历史材料和推断。不要把一次 MCP 写入成功说成性能已优化，也不要在未运行测试时声称行为已经验证。

### 本次具体施工交互中的工具反馈

- 首次尝试一次提交 9 节点时，因为依据超过 32 条被拒绝；压缩依据后继续。
- 随后提交分别被无效节点 ID、`runtime-wiring.ts` 证据过期、错误复制 `state.ts` 文件 ID 阻止；每次错误均拒绝整个 batch，最终一次写入成功。
- 成功路径是重新批量预览依据文件、从 `list_files` 按路径动态解析所有文件 ID、预先检查令牌，再一次提交并用 `search` / `context` 验收。

这些结果说明确认批次的端到端工具链有明确能力护栏，但 Agent 可通过自动路径解析、token 刷新和 batch 预算预检减少无效写入尝试。超限和过期错误本身不应被弱化；应改善预检与错误聚合。

## 裁定记录

| 待裁定问题 | 建议 | 本项目决定 | 依据/待验证项 |
| --- | --- | --- | --- |
| “固化”要缓存或物化什么？ | 先分 condition truth、active Entry 集、flow/zone 派生值与持久化状态，不合并成一个笼统缓存。 | 待裁定 | 当前 AffectorInstance 为运行时派生状态；核对 `AffectorInstance` 生命周期与恢复路径。 |
| 哪些条件允许退出每 Tick 轮询？ | 只在依赖可追踪且所有写入路径都能发出对应失效事件时退出；保留未知依赖的保守回退。 | 待裁定 | 需要枚举 `ConditionDepIndex` 每种 target、事件写入点和宿主重建路径。 |
| 性能改进验收如何量化？ | 同时测量每 Tick 条件评估数、每次事件受影响实例数、状态翻转正确性和总 Tick 结算，不只比较墙钟时间。 | 待裁定 | 需选择代表性数据规模、Profiler/计数工具及可重复基准。 |
| GALHCG 如何表达上下位概念？ | 比较 Concept→Concept 层级边与 `level` 元数据；要求支持横向有向边、祖先/后代查询和无环验证。 | 待裁定 | 当前节点类型与关系验证不支持 Concept 层级；需要先定义兼容的查询与存储契约。 |
| 是否同一施工任务实现两类变化？ | 初步倾向分开验收 Affector 性能变化与 GALHCG schema / 查询变化；本 Draft 将它们作为同一探索中发现的两条方向记录。 | 待裁定 | 两者可独立交付，且分别涉及 Engine 与 MCP 服务边界。 |

## Draft → Task 准入

- [ ] “固化”的状态对象、生命周期和非目标已裁定
- [ ] 目标 Condition、事件覆盖矩阵及所有写入/恢复路径已核对
- [ ] Affector 性能验收指标、代表性规模与正确性测试已定义
- [ ] GALHCG 层级 schema、允许关系、遍历 API 与迁移/存量图行为已裁定
- [ ] 决定 Affector 与图谱改造是否拆成独立 Task，并写明依赖
- [ ] 为每个 Task 给出独立完成定义、最小 Read Set 与验证入口

## 下一步

1. 继续做只读事实核对：从 `ConditionDepIndex` 条件叶子映射追到每种事件写入点、Affector 重建路径与对应测试，形成“可精确失效 / 暂需轮询 / 需人工裁定”矩阵。
2. 为 GALHCG 画一版可执行 schema 草案，至少覆盖三层概念、横向关系、文件/测试依据、路径查询、无环约束和更新预算；先用 Affector 需求做查询验收样例。
3. 完成裁定后按责任边界另立实施 Task；本 Draft 继续记录语义和事实问题，不把方案建议当成已实现事实。

## 相关路由

- [[docs/docs-829/00-INDEX]]
- [[docs/docs-829/01-architecture/dependency-map]]
- [[docs/docs-829/04-mechanisms/engine/effect-trigger]]
- [[docs/docs-829/05-conventions/architecture-discipline]]
- [[task-0034-affector-performance-review]]
