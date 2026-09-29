# ACProgram 语义图挖掘质量改进建议

状态：Draft — 只读筛查与挖掘规范建议；不代表已批准重构 ACProgram 图或修改服务代码  
核对日期：2026-09-29  
适用项目：`acprogram` → `C:\Users\15229\Documents\Obsidian Vault\1-项目\ACProgram`

> 本文讨论语义地图是否便于 Coding Agent 导航源码。它不修改 ACProgram 源码、GALHCG 服务代码或数据库；Affector 性能改造仍须单独裁定。

相关记录：[ACProgram 试用原始 Draft](ACProgram试用原始Draft.md)、[GALHCG 试用反馈改进任务](ACProgram试用反馈改进任务.md)、[Affector 案例试用结果](verification/affector_case_report.md)。

## 1. 结论

当前问题不是“地图没有挖到内容”，而是“挖到的机制知识没有组织成最适合代码导航的形状”。Concept 的评价标准应是：**能否帮助 Coding Agent 快速找到正确的代码入口并理解其职责**，而不是图是否更深、更均匀或节点更多。

Concept 不等于代码符号，但应优先锚定稳定的数据结构、类或导出/公共行为函数。跨多个实体的机制型 Concept 仍可保留，前提是它有稳定的导航价值，并能明确指出由哪些代码实体共同实现。这样既避免宽泛的文档标题成为概念主体，也避免把语义图做成 AST/符号索引器。

## 2. 本次筛查基线

以下是 2026-09-29 对本机 `acprogram` 数据的只读查询结果；之后若 Agent 已重构地图，应重新读取 MCP `status`、`context` 和 `traverse`，不要沿用旧计数。

| 项目 | 本次观察 |
| --- | ---: |
| 已索引文件 | 1,180 |
| Module / Concept | 6 / 18 |
| 语义边总数 | 154 |
| `contains` | 56：Project→Module 6、Module→Concept 20、Concept→Concept 8、Module→File 22 |
| `maps_to` | 81 条，涉及 61 个不同文件 |
| `depends_on` / `related_to` | 9 / 8 |
| 已存文件依据 | `status` 当时报告 70 个不同依据文件；18 个 Concept 均为 `confirmed` 并有依据 |

这些数量说明地图已有实际文件关联和有证据的机制内容，不能单凭“覆盖了 61 个文件”推断理解完整或准确。多数 Concept 摘要内容具体；主要问题是概念标题和边界太偏主题总结，代码锚点不够突出。

## 3. 质量筛查

### 已有价值

- 六个 Module 大体对应清晰的架构边界：AronaClicker 领域、Data Services、应用与 Runtime 组合、游戏 UI、独立 Datapack Editor、通用 Engine。
- 多数摘要写到了实际状态、事件或行为；例如 Affector 条件新鲜度、`ConditionDepIndex` 的定向失效、stat/未知依赖的轮询回退，以及 Tick 资源结算。
- Affector 生命周期下的活跃贡献、条件新鲜度与显式 `perTickEffects`，以及条件依赖到 Affector/Visibility 的联系，已形成可进一步核对的机制路径。

### 主要问题

1. **标题常以主题命名，符号锚点退到摘要或文件边。** 例如“状态写入与事件派生协调”摘要引用了 `StateMutationService` 和事件系统，但节点名没有直接呈现代码入口；“条件依赖索引与失效”也可从 `ConditionDepIndex` 的职责出发组织。
2. **相邻机制可能重复概括。** “运行时内容生命周期”“Datapack 到 Registry 的内容流”“启用包应用与热重载”覆盖范围相邻；“状态写入与事件派生协调”“状态写入与只读视图边界”“UI 命令到 Runtime”也都处于状态到界面的流程中。需要根据实际类、函数、调用与数据流厘清边界，不预设合并或拆分。
3. **少数横向边的语义可能过强或依据不足。** 8 条 `related_to` 中有一条当时没有关系依据；Concept 间的 `depends_on` 也应检查是否真是直接依赖，还是仅表示同一机制中的关联。
4. **部分 Concept 的直接依据仅为文档。** 文档可解释设计，但代码导航型结论应尽量由当前实现和测试支撑。`maps_to` 关联源码并不等同于该 Concept 的摘要已由源码核实。
5. **层级浅并非本身缺陷。** 当前 18 个 Concept 中有 8 条 Concept→Concept `contains` 边，存在多个叶节点是合理的；不应把增加层级当成挖掘质量目标。

## 4. Concept 身份与摘要准则

### 主体选择

- 优先考虑稳定的数据结构/类型、类、导出函数或具有明确公共职责的行为函数。
- 代码符号只有具备稳定职责和导航价值时才成为 Concept。不要为每个私有 helper、小方法或 AST 符号建节点。
- 机制型 Concept 只有在跨实体协作本身值得单独导航时保留；摘要需指出实现它的代码实体及它们之间的关系。
- 保留代码中的准确英文标识符作为主体名；中文说明可以放入别名或摘要。
- 节点粒度以“是否帮助找到代码入口、理解职责”为准。不要为图的深度、均匀度或视觉完整性拆分节点。

### 摘要写法

以主体标识符开头，简洁说明：

1. 它负责/持有什么状态或数据；
2. 主要接受的输入、事件或调用；
3. 产生的输出、副作用或派生结果；
4. 关键不变量、回退路径或边界。

避免只复述文档章节标题，或以“负责某某生命周期/流程”结束但不说明代码责任。多实体机制应写出核心实体之间的协作方式，而不是一段无主语的系统概述。

### 可优先核查的源码锚点

下列标识符是筛查时在当前源码中见到的候选，不是必须全部建立为 Concept 的清单：

- 数据结构与初始化：`PlayerState`、`InitSnapshot`、`PER_INIT_FIELD_SPECS`、`InitSavepoint`
- 状态与只读视图：`StateMutationService`、`buildGameView`、`UIContext`
- 内容加载与解析：`PackManager`、`RuntimeContentCoordinator`、`Registry`、`resolveDefinition`
- 条件与派生系统：`ConditionDepIndex`、`collectConditionLeaves`、`AffectorInstance`、`AffectorEngine`、`VisibilityIndex`、`VisibilityEngine`
- Tick 行为：`TickSystem` 与资源结算入口

建立节点前仍须检查当前源码和测试，确认标识符存在、当前职责成立，并选用合适的实体粒度。

## 5. 结构边、横向边与文件依据

- `contains` 只表示真实的所有权、组成或职责分解。Project→Module、Module→Concept，以及有事实依据的 Concept→Concept 均可；不为凑层级制造父子关系。共享节点可有多个父节点。
- `depends_on` 只表示源码或数据流可验证的有向直接依赖。不能把主题相近、共同参与某个流程直接标为依赖。
- `related_to` 用于有意义但非直接依赖的联系；每条边应有文件依据，Agent 在最终报告中说明联系理由。不能以它替代已能明确表达的调用、消费或失效关系。
- `maps_to` 仅关联实际支撑节点的文件，并准确标注 `implementation`、`test` 或 `documentation` 角色。实现和测试优先，文档补充解释；不要为了提高文件计数泛挂文件。
- `confirmed` 表示本次记录有当前有效依据，不等于内容必然正确。提交前应从 MCP `preview` 获取每条依据的当前 `version_token`。

## 6. 三组相邻主题的复核办法

对以下主题分别检查源码中的责任、调用与数据流，再决定保留、重锚、合并或降为摘要说明：

| 现有主题 | 优先核查的代码入口 | 判断问题 |
| --- | --- | --- |
| Datapack→Registry 内容流 | `PackManager`、`Registry`、`resolveDefinition` | 是否专指静态内容从加载到可查询结构的流程？ |
| Runtime Content 生命周期 | `RuntimeContentCoordinator` 及 `Runtime` 装配 | 是否有独立的创建、修改、撤销或重载协调职责？ |
| 启用包应用与热重载 | 包启用计划、Coordinator、Registry 更新入口 | 是否存在独立且可单独导航的实现流程，还是前两项的重复概括？ |

“状态写入与事件派生协调”“状态写入与只读视图边界”“UI 命令到 Runtime”也按同一方式核对，分别检查 `StateMutationService`、`buildGameView`、`UIContext` 和 UI 到 Runtime 的命令入口。不要仅凭标题相似就合并。

## 7. Agent 操作与验收流程

1. 开始前用 `status(project_id="acprogram")` 确认当前根目录、索引和节点数量。项目 ID 或根目录冲突时重查并报告，不能按 `default` 猜测。
2. 对每个现有 Concept 记录保留、重锚、合并、删除或待核实的建议；先读源码/测试，再参考当前文档。历史文档与归档性能记录只作历史路由，不作为当前实现证据。
3. 保留仍有意义节点的稳定 ID。若合并或替换节点，先规划如何迁移有效摘要、依据、文件角色与边；不得无说明丢失已有事实。
4. 更新文件映射前，用 `resolve_paths` 按项目内路径解析 File ID；每条证据从 `preview` 取得令牌。按单次最多 32 条 evidence references 预算拆批，并先确保批次端点存在。
5. 用 `update_map(dry_run=true)` 检查节点字段、端点、重复边、文件版本与 contains DAG。只有预检有效后才提交实际更新；不要将预检错误变成跳过校验或部分写入。
6. 写后用 `search(map)`、关键节点 `context` 与有界 `traverse` 检查名称可发现、职责摘要、项目隔离、结构路径、横向边方向、文件角色、依据新鲜度和结果完整性。
7. 报告现状事实、源码核实结果、历史材料、推断与未核实事项；不要因图更新成功就声称 Affector 已优化或性能已提升。未经实际运行测试，不声称测试通过。

## 8. Affector 案例边界

Affector 用作检验代码导航价值的案例，不授权运行时改造。已有记录指出：当前实现包含事件依赖定向重估、stat/未知依赖轮询回退、Active Entry 的显式 `perTickEffects` 和独立的 Tick 资源结算；Affector 文件头注释与方法实现/当前机制说明存在冲突。任何后续方案都应回到当前实现和测试核对完整调用链，不沿用 `task-0034-affector-performance-review` 归档内容作为现状。

“固化”仍待定义，不能默认解释为持久化存档。条件结果、Active Entry 集、flow/zoneModifier 等派生贡献是不同对象；哪些可以停止轮询、哪些写入路径保证失效通知、性能收益如何验收，均属于独立的事实调查与裁定事项。

## 9. 明确不在本文批准范围内的工作

- 不修改 GALHCG 服务代码、schema、MCP 接口、UI 或数据库。
- 不因语义图质量建议直接修改 ACProgram 源码、测试或文档。
- 不将所有 1,180 个已索引文件转成 Concept，也不以地图关联文件数作为理解覆盖率。
- 不增加 `level` 字段或新的关系类型，除非后续有独立契约、兼容与迁移方案并经裁定。
- 不把“图更深/边更多/摘要更长”作为成果指标。

下一步仅建议将第 4–7 节作为 ACProgram Agent 的重构图指令与验收标准。实际 MCP 图更新须由明确的 Agent 操作完成，并在单独记录中保留更新前后统计、预检结果及回读验收。
