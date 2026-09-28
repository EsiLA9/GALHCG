# ACProgram 试用反馈：GALHCG 改进任务

> 状态：G01–G07 已实施并完成验收；最终契约、回归结果与收益边界见第 13 节。
> 核对日期：2026-09-28。主项目：GALHCG；ACProgram 源码、文档与测试保持只读，验收使用临时副本和临时数据库。
> 输入：[ACProgram 试用原始 Draft](ACProgram试用原始Draft.md)。实施总入口：[项目任务列表](项目任务列表.md)。

## 1. 交付目标与责任边界

让 Agent 从“Affector 条件新鲜度”这一需求进入概念图，沿系统职责、管理机制、具体机制找到当前实现、解释文档和测试，并知道路径是否完整、依据是否仍有效。建图过程应能在提交前定位批次错误，减少文件 ID、依据数量与过期令牌造成的重复尝试。

本轮保留服务的基本分工：服务管理受限查询、元数据和版本校验；Agent 阅读和理解源码。支持局部建图，不要求给全部文件建立概念，也不引入全量 AST、向量库、自动总结或后台重建。

本文件记录 GALHCG 的变更与验收结果。Affector 的运行时缓存、精确失效、持久化语义与性能优化另属 ACProgram；它们不是本轮实现依赖，也没有因图谱验收而被视为完成。第 4–10 节的接口建议已落地；最终契约和实现边界见第 13 节。

## 2. 调查记录与事实修正

### 2.1 本次真实 MCP 复核

本次仅调用 `status`、`search` 和 `context`；未刷新清单、未更新现有图。以下统计是工具返回的既有成功刷新记录，不是本次重新扫描的测量。

| 项目 ID | 根目录末段 | 文件 / 目录 | 文件元数据字节合计 | 扫描耗时 | 范围 / 状态 |
| --- | --- | --- | --- | --- | --- |
| `default` | `1-项目/GALHCG` | 23 / 4 | 335,998 | 4 ms | 根目录 / succeeded |
| `acprogram` | `1-项目/ACProgram` | 1,179 / 114 | 11,268,593 | 264 ms | 根目录 / succeeded |

`status` 本次返回两个完整根目录，均未截断。原 Draft 首次只见 `default` 的原因仍未知；当前正常返回不能证明先前冲突已修复。`ProjectIndex.status()` 只列本次服务配置的项目，数据库中的其他项目不会自动成为当前可用项目。

| 调用 | 本次结果 | 结论 |
| --- | --- | --- |
| `search(acprogram, map, "Affector")`，默认大小写敏感 | 0 条 | 需记录搜索参数，不能把这次无命中当作文件不存在 |
| 同上，`case_sensitive=false`、`limit=100` | 18 条，全为 File，无截断 | 复现 Draft 的文件候选；包含 docs-828 与归档任务，没有 Affector Concept |
| `search(acprogram, map, "src/engine")` | 0 条 | 完整路径不是当前 map 搜索字段 |
| `context(acprogram, "module:engine", neighbor_limit=20)` | 父节点为 ACProgram；两个文件子节点为当前 Engine 模块卡和 `src/engine/index.ts`；三个入向模块依赖 | 模块入口存在，但不能沿该入口下钻到 Affector 管理概念 |

原 Draft 的两次写图数量和失败重试过程作为用户提供的试用记录保留，本次没有重演写入，未重新统计整图。不能把上述只读结果表述为重新完成端到端建图。

### 2.2 GALHCG 源码确认的限制

| 编号 | 当前事实 | 源码入口 | 对任务的影响 |
| --- | --- | --- | --- |
| F1 | `contains` 允许 Project→Module/File、Module→Module/Concept/File；禁止 Concept→Concept | `semantic_map.py::ALLOWED_ENDPOINTS` | 支持概念分解需要改合法端点与结构校验 |
| F2 | 环检测 SQL 只纳入 Project/Module 两端 | `SemanticMap._check_contains_cycles()` | 仅开放端点会漏检概念环，必须同步修改 |
| F3 | `related_to` 规范化端点后按无向边保存；`depends_on` 仅支持 Module→Module | `_parse_edge()`、`ALLOWED_ENDPOINTS` | 弱相关不能冒充有向依赖或因果关系 |
| F4 | `context` 从受限邻居推导 parents/children；追溯时每个当前节点只查一个父节点；所谓 depth 常量实际限制祖先数量 | `SemanticMap.context()` | 当前已有有限祖先查询，但不能保证多父图完整性，也没有通用多跳路径查询 |
| F5 | 每条边的依据只查询前 4 条，未独立报告被省略的依据；邻居与结构无分页续读参数 | `SemanticMap.context()` | 不能仅用全局 `truncated=false` 解释为依据与路径完整 |
| F6 | map SQL 搜索 name/summary/aliases；File.name 是 basename，path 只随结果返回；map 拒绝 directory 参数 | `SemanticMap.search()`、`IndexStore._ensure_file_node()`、`server.search()` | “返回路径”和“搜索路径”必须分清；需要类型筛选和命中解释 |
| F7 | source 正文搜索已经存在 | `filesystem.py`、`server.search()` | 不另立“从零实现正文检索”；任务是路由、范围收窄与证据解释 |
| F8 | 32 条上限统计所有节点与边的 evidence 条目之和，同一路径在不同 owner 上出现会重复计数 | `SemanticMap.update_map()` | 引用条目预算不能误称为“32 个唯一文件” |
| F9 | 依据校验按路径缓存，提交前还会再次计算版本；总哈希预算 256 MiB、更新时间预算 15 秒；当前遇错即返回 | `_verify_evidence()`、`update_map()` | 预检不能放松正式提交校验；错误聚合也必须有预算 |
| F10 | context 的 `missing_file` 根据持久化清单查询得出，不是每次探测磁盘；不检查已存依据当前版本 | `SemanticMap.context()` | 删除但未 refresh 的文件仍可能显示 File；应接回原阶段 5 |
| F11 | 当前数据库 schema 为 v4；关系枚举也受 SQLite CHECK 约束 | `store.py::SCHEMA_VERSION`、`_migrate_to_v2()` 至 `_migrate_to_v4()` | 原阶段 4 的 v2 记录是历史节点；新迁移必须从当前 v4 及受支持旧版本测试 |

这里的源码文件均位于 `src/project_preview/`。导出工具 `export_map_markdown.py` 另有关系标签和依据读取逻辑，schema/展示变化时必须同步检查，不能只改 MCP 返回。

### 2.3 ACProgram 样例事实的复核范围

已阅读 ACProgram 的 `AGENTS.md`、当前文档索引、Affector 机制文档，并检查 AffectorEngine、ConditionDepIndex、TickSystem 和事件驱动测试的相关正文。

- `registerConditionDeps()` 将 stat/未知 target 条件实例加入 `pollingInstances`；`applyActiveEffects()` 轮询这一集合，然后只执行 Active Entry 的显式 `perTickEffects`。文件头“每 tick 全量重估”与实现冲突仍存在。
- `ConditionDepIndex.addLeaf()` 除 Draft 所列依赖外，还处理 `tagCount`、`hasReadStory`、`hasReadStoryInRun`、`visitedStoryInChain`；后续 ACProgram 调查不能只用原 Draft 的枚举当作完整矩阵。
- `event-driven-reactor.test.ts` 已有 flag 事件翻转、跨实体 spotLevel 响应和 stat 轮询回退断言。读到测试不等于测试已在本次通过。
- `TickSystem.tick()` 还结算资源产出并发出事件；条件重估、显式逐 Tick 效果与资源结算必须作为不同概念。
- 当前机制文档在 `docs/docs-829/`；`docs-828`、`docs-824` 和 `docs/plan-work/archive/` 是历史材料。这是 ACProgram 的项目规则，不能硬编码成 GALHCG 对所有项目的通用目录规则。

本次未运行 ACProgram 测试，未完成所有状态写入点与恢复链的追踪，也没有 Affector 性能基准。因此本文件不决定任何条件可以取消轮询。

### 2.4 本次验证基线

在 GALHCG 工作目录显式设置 `PYTHONPATH=src` 后执行现有 `unittest discover -s tests -v`：48 项，47 项通过，1 项 Windows 符号链接测试因缺少创建权限跳过。包含 11 项语义地图测试和 2 项真实 SDK stdio 测试。测试使用临时项目；未修改现有项目图。

这只是调查阶段的基线。本轮新行为的最终回归结果及数据库版本迁移见第 13 节。

## 3. 分期任务与依赖

| ID | 优先级 | 独立交付 | 依赖 | 状态 |
| --- | --- | --- | --- | --- |
| G01 | P0 | 查询完整性、参数与项目身份诊断 | 无 | 已完成 |
| G02 | P0 | 有界依据新鲜度与覆盖报告 | 接续原阶段 5；采用 G01 完整性约定 | 已完成 |
| G03 | P1 | Concept 层级及最小有向依赖 | G01；先冻结关系契约 | 已完成 |
| G04 | P1 | 有界多跳遍历与可续读路径 | G03；新鲜度接入 G02 | 已完成 |
| G05 | P1 | 文件角色、概念搜索与来源路由 | G03；路径验收接 G04 | 已完成 |
| G06 | P1 | 批次预检、精确文件定位与错误聚合 | G01；复用 G03/G05 校验器 | 已完成 |
| G07 | P1 | Affector 案例及收益评估 | G01–G06；归入原阶段 6 | 已完成 |

实施顺序：G01 → G02 → G03 → G04 → G05 → G06 → G07。各项已分别验收；Affector 运行时性能任务仍属于 ACProgram，不在 GALHCG 依赖链中。

## 4. G01：查询完整性与使用边界

**问题：** 有限邻居可能漏掉结构入口，依据被省略却无法续读；搜索参数和项目身份不够容易留存复现。

**施工内容：**

- [x] 为 neighbors、parents、children、ancestors、node evidence、edge evidence 分别说明是否完整、截断原因和继续方式。无分页能力的区段明确提示限制；G04 再交付统一遍历续读。
- [x] 结构入口单独查询，不再依赖横向邻居页是否恰好包含父边；保留现有字段并新增完整性信息，避免破坏旧调用。
- [x] README 与工具说明明确 map 的字段、大小写默认值、参数上限和 directory 不适用范围；明确当前 map 的 query 上限为 256 字符，不能直接沿用 source/path 的 512 字符描述。
- [x] 项目选择失败和模式参数失败响应尽可能返回已确定的 project_id；所有复现实验记录项目 ID、根目录、模式、大小写和范围。根目录截断时不能以显示前缀判断身份。
- [x] 身份冲突时记录连接/启动配置与完整 status，再重查。是否新增服务实例或配置标识待真实冲突复现后决定，不预设根因。

**验收：**

- [x] 一个节点超过 20 条邻边时，父结构仍可查，所有省略有明确标记；`neighbor_limit=0` 不被解释为“没有父节点”。
- [x] 一条关系有 5 条依据时，显示全部或报告依据截断和继续方式；10 层以上、多父结构同样不会静默省略。
- [x] 默认 `Affector` 与大小写不敏感查询的差异可复现；接口说明与实参限制一致。
- [x] 两个项目同名节点、同相对路径及缺省 ID 调用仍正确隔离。

**最小 Read Set：** `semantic_map.py::context/search`、`server.py::search/_select_project`、`index.py::status`、`tests/test_semantic_map.py`、`tests/test_mcp_stdio.py`。

## 5. G02：接续阶段 5 的可信维护

**问题：** confirmed 只代表 Agent 提交时的理解状态，不能代表现在的内容版本仍匹配；清单未刷新时也可能掩盖磁盘删除。

**施工内容：**

- [x] 节点与边分别返回 `fresh / stale / unknown`、检查范围和原因。已发现任一依据变化或删除即 stale；存在依据且全部检查匹配才 fresh；无依据、超预算或访问失败且未发现变化时 unknown。
- [x] 限制单次检查文件数、累计字节和时间，并在响应中给出本次使用量。跨 owner 共享文件的读取可以去重，结论仍按各 owner 保存的版本比较。
- [x] freshness 只说明此次检查时版本匹配，不证明语义正确；查询结果展示截断与新鲜度检查不完整分别表示，不因只展示部分依据就宣称全部 fresh。
- [x] refresh 不重写语义依据，不清除 stale；重新阅读后由 Agent 经 update_map 替换依据。沿关系不无限传播过期。
- [x] status 分开报告文件清单、已建立语义关联范围、已知 stale 与未检查项。覆盖统计排除自动 Project→File 边的“全覆盖”假象；目录新增只提示结构可能需要复核。

**验收：**

- [x] 内容修改且恢复原 mtime/大小仍检出 stale；仅触碰 mtime 不误报内容变化。
- [x] 删除但未 refresh、删除后 refresh、无法读取、无依据、超预算分别有正确结果。
- [x] 边依据变化可以单独呈现；无关概念不被整库污染；刷新清单不能使 stale 自动 fresh。
- [x] 重读并更新后恢复 fresh；状态与版本校验预算均有边界测试。

**最小 Read Set：** 原任务列表阶段 5、`semantic_map.py::_verify_evidence/context`、`versions.py`、`filesystem.py` 的版本计算、`store.py` 依据表与 status、既有过期令牌和文件删除测试。

## 6. G03：Concept 层级与最小有向关系

**最终契约：** 保留四类节点；扩展现有 contains，而非另建一套并行概念树。Concept 支持多父 DAG，跨模块复用同一稳定 ID。层数不固定，不新增强制 `level`；domain/management/mechanism 用于编写约定，不按深度推断业务语义。

| 关系 | 建议允许端点 | 含义与约束 |
| --- | --- | --- |
| `contains` | Project → Module/File；Module → Module/Concept/File；Concept → Concept | 上位职责分解到下位机制；全结构无环，允许多父 |
| `depends_on` | Module → Module；Concept → Concept | 源概念依赖目标概念；有向，可有业务依赖环，遍历必须去重 |
| `maps_to` | Concept→File | 文件关联，角色由 G05 描述 |
| `related_to` | Concept↔Concept | 保留无向弱相关；不据此推导因果 |

`invalidates / triggers / derives / consumes` 暂不全部加入。先用依赖边表达本案例的可核实关系；如验收确需区分其中某类，追加一条关系的精确定义、方向、合法端点、证据要求和用例，再修改 SQLite CHECK。不能在摘要写了“触发”后就把普通依赖解释成已证实触发关系。

**施工内容与验收：**

- [x] `_check_contains_cycles()` 覆盖扩展后的全部结构节点；两节点、长链和同批新增/修改导致的环均拒绝，整批回滚。
- [x] 支持“系统概念→管理概念→机制概念”三层及第四层；同一机制由两个父概念引用，两条路径均可读取。
- [x] 删除父概念只级联其关系与自有依据，不删除共享子概念；删除中间节点后不自动猜测重接边。
- [x] 结构边与横向边分开展示；反向查询保留原 source/target，不能反转存储语义。
- [x] 旧节点、关系与调用保持可用。若只改合法端点，不无故增加数据库版本；新增字段/枚举需要版本化事务迁移，禁止直接编辑用户库结构。
- [x] 迁移保留项目、文件 ID、语义 ID、依据、版本令牌 secret 与刷新历史；从当前 v4、受支持旧库及迁移失败场景验证。新版本库交给旧程序时须明确拒绝，不能静默损坏。
- [x] 地图 Markdown 导出可表达概念父子与方向，不要求读者从一个扁平文件清单猜结构。

**最小 Read Set：** `semantic_map.py` 解析、合法端点与环检测；`store.py` 迁移/外键；`export_map_markdown.py`；语义地图关系、删除、隔离、迁移测试。

## 7. G04：有界多跳遍历

**最终契约：** 保留 context 作为局部卡片入口；新增只读 `traverse` 供路径探索。

- 请求指定 `project_id`、`start_node_id`、可选 `target_node_id`、关系集合、`direction=outgoing/incoming/both`、`max_depth` 和结果预算。
- 默认只沿 contains 下钻；横向 depends_on 或 related_to 必须显式纳入。深度按边数定义，起点深度为 0。
- 默认深度 2、硬上限 8；每页最多 50 节点、100 边，JSON 正文不超过 48,000 字节。访问上限为 1,000 节点、2,000 边、2 秒，限制数据库工作量而不只限制返回量。
- 返回节点与边、原始方向、可重建路径、`complete`、停止原因和续读信息。预算内没有到达目标只能说“本次未找到”，不能宣称整个图不连通。
- DAG 节点只展开一次，但保留已探索到的多父边；含横向环不会无限展开。不枚举指数级全部路径；指定目标时可返回一条确定的最短路径，并声明范围与选择规则。
- 游标绑定 project、查询条件和图修订号；图变化或跨项目复用时拒绝游标并要求重启。图修订号覆盖语义更新及刷新引起的受管节点/边变化。

**验收：**

- [x] 四层概念、菱形多父结构、20 个以上邻居、依赖环和孤立节点均在预算内正确终止。
- [x] 固定图分页无重复遗漏，顺序确定；查询中途写图后拒绝失效游标；不能跨项目复用游标。
- [x] 输出每一步的 relation、source、target；按反向查询走到的边不会被改写成相反依赖。
- [x] 深度、访问量、时间和输出字节截断可区分；没有假完整或无法继续的无提示省略。
- [x] 从系统概念能到达 Affector 条件依赖，再经 maps_to 到 ConditionDepIndex 的实现和测试。

**最小 Read Set：** G03 契约、`semantic_map.py::context`、`store.py` 图索引与事务、`index.py`/`server.py` 工具封装、stdio 测试。

## 8. G05：文件角色、概念发现与当前来源路由

**建议契约：** 文件身份保持唯一；角色放在概念与文件的关联上。同一文件可以为不同概念承担不同角色，不给 File 全局指定唯一“实现/测试”类型。

- [x] maps_to 增加可选 `roles`：`implementation / documentation / test / unspecified`。旧边缺省为 unspecified；同一概念文件对可有多个角色，沿用现有唯一边键，不复制 File 节点。
- [x] roles 与版本 evidence 分开：关联角色描述文件用途，依据证明 Agent 读过哪个版本。缺角色不阻断旧图，缺版本不能伪装为当前已确认依据；关联目标是 File 也不等于该目标已被预览。
- [x] map 搜索增加节点类型筛选与 `matched_fields`，默认行为保持兼容。可只查 Concept/Module，避免一般文件挤占语义入口。
- [x] Agent 默认先显式进行不区分大小写的概念搜索；无概念命中时说明未建该主题图，再转 path/source/preview，不能以文件命中声称找到了语义概念。
- [x] 第一轮继续用 path 搜索完整路径，不悄悄把 map 改成源码/路径混搜；若新增路径条件，须明示其作用域并补充专门测试。
- [x] 当前/历史来源遵循目标项目明确规则。首轮由 Agent 工作流程标注，不自动按目录数字或时间猜新旧；历史文档可以保留检索，但不得成为“当前实现”唯一证据。
- [x] Coverage 说明已建立语义关联的范围及未建图部分；不要把文件覆盖率写成理解准确率。

**验收：**

- [x] 搜索 Affector 得到有别名与摘要的 Concept；只查 Concept 时不会混入 18 个普通 File。
- [x] 可区分源文件、当前机制文档、测试和历史材料；结果显示为什么命中，仍能转 preview 阅读。
- [x] 相同文件可被多概念引用且角色不同；旧 maps_to 迁移后仍可查询，角色未被自动猜测。
- [x] 来源路由无法判定时明确未知；没有覆盖某主题不阻断普通文件检索。

**最小 Read Set：** `SemanticMap.search/_parse_edge/context`、`store.py::_ensure_file_node` 及 map_edges、`server.py::search`、地图导出、ACProgram `AGENTS.md` 的文档路由规则。

## 9. G06：批次预检、路径解析与原子提交

**问题：** 当前逐次遇错返回造成反复提交。需要提前解释错误与预算，而非提高上限或允许部分错误被跳过。

**建议契约：** `update_map` 增加只读 `dry_run`；与正式提交共享解析、端点、图结构及版本校验器。另提供批量精确路径解析入口，或给现有 list_files 增加等价精确解析能力，实施时二选一并固化文档。

- [x] 路径解析在选定项目内返回 canonical path 和 node_id；遵守现有忽略、越界、链接和大小写规则。未入清单时提示局部 refresh，不私自全盘扫描；不接受猜测的跨项目 ID。
- [x] 预检报告各操作数组数量、总 evidence 引用数、唯一路径数、上限、已检查范围和实际检查成本。同一路径在两个 owner 中出现算 2 条引用；单 owner 内重复路径仍属错误。
- [x] 聚合独立可判定错误，返回操作数组、下标、字段、错误代码及修复建议。某项格式坏掉时跳过依赖它的验证，并报告未检查项；因预算中断不能返回“完全通过”。
- [x] 建议错误展示硬上限 50 条且遵守 48,000 字节预算，超出时报告错误列表不完整；不要承诺“所有错误”无上限返回。
- [x] `dry_run` 不提交任何图变化，也不保存“已通过”的永久票据；正式提交始终重新校验版本、端点及环，并保留提交前版本复核。
- [x] 先组好批次和解析文件 ID，再集中 preview、更新证据、预检、提交。文件发生变化后须重新阅读并确认摘要/关系仍成立，不能只刷新令牌覆盖旧结论。
- [x] 超限时优先按概念分批或复用节点，不为通过预算删去必要证据。每批原子，多个批次不构成整体原子事务；失败时列出已成功批次及剩余步骤，不能冒称整图未改变。

**验收：**

- [x] 32 条引用通过数量预检，33 条明确失败；即使仅引用 1 个唯一文件也按 33 条计算。
- [x] 同批无效 ID、错误文件端点、过期 token 等可独立判定问题被聚合；无效项不会让其他合法项意外入库。
- [x] dry_run 前后图及依据相同；dry_run 成功后再修改文件，正式提交必须失败且整批无变化。
- [x] 预检成功后发生节点删除、增加成环关系或清单变化，正式提交再次验证，不能使用过期预检绕过限制。
- [x] 15 秒与哈希字节预算、错误输出上限仍有效；预检和正式提交均不对目标源码写入。
- [x] 批量路径解析避免手抄 ID；两项目相同路径解析到各自节点。

**最小 Read Set：** `SemanticMap.update_map/_verify_evidence/_allowed_relation`、`store.py::transaction/list_files`、`map_ids.py`、`index.py::preview`、`server.py::update_map` 和原子性/版本测试。

## 10. G07：以 Affector 建立可复现验收案例

### 10.1 最小案例图

以下为建议语义命名，实施时按当前源码确认后再建图；测试使用隔离数据库。真实项目试用保留现有图，不用覆盖式导入重置已有理解。

```text
module:engine
└─ concept:engine-continuous-rules（持续规则系统）
   └─ concept:affector-freshness（生命周期与条件新鲜度）
      ├─ concept:condition-event-invalidation（事件依赖登记与定向重估）
      ├─ concept:condition-polling-fallback（stat / 未知依赖轮询回退）
      ├─ concept:active-entry-transition（Entry 激活沿与一次性效果）
      ├─ concept:explicit-per-tick-effects（显式逐 Tick 效果）
      └─ concept:derived-contributions（flow / zone 等派生贡献）
```

结构边均使用 contains。管理概念及机制概念通过 maps_to 指向下表文件，按用途标角色；节点与重要关系引用真实阅读取得的依据。跨层依赖仅在阅读确认后增加，不用命名相似自动推导因果。

| 机制 | ACProgram 最小阅读与验收入口 |
| --- | --- |
| 事件失效 / 轮询回退 | `src/engine/expression/condition-deps.ts`；`src/engine/effect/affector-engine.ts` 的注册、事件回调与 applyActiveEffects；`tests/engine/event-driven-reactor.test.ts` |
| Active Entry / 激活沿 | `src/engine/effect/affector-engine.ts::recheck`；`tests/engine/affector-engine.test.ts` |
| 挂载恢复 | `src/engine/effect/affector-engine.ts::reconcileMounts`；`tests/engine/affector-reconcile.test.ts` |
| Tick 与派生贡献边界 | `src/engine/system/tick-system.ts`；当前 `docs/docs-829/04-mechanisms/engine/effect-trigger.md`；相关 GameNum 实现和测试由该文档继续路由后确认 |

对尚未完成实现/文档/测试三方核对的机制保持 tentative 或明确覆盖缺口，不为满足“三类文件齐全”挂上无关测试。

### 10.2 固定验收问题

- [x] “Affector 是否每 Tick 全量重估？”能从概念入口得到事件重估与轮询回退两条路径及具体实现，指出陈旧文件头冲突。
- [x] “为什么 stat 还需 Tick？”能到达 fallback 机制、ConditionDepIndex 与已有 stat 测试；不把测试存在解释为所有写入路径已证明安全。
- [x] “停止条件重估是否等于停止资源产出？”能区分条件判定、perTickEffects、持续 flow 和 TickSystem 结算。
- [x] “这个结论依据还新鲜吗？”修改临时 fixture 文件后返回 stale，并能定位需要重读的节点或边。
- [x] “某个区域还未建图怎么办？”明确未覆盖，能继续从 source/path 定位，无伪造的语义路径。
- [x] 所有路径保持同一 project_id；GALHCG 项目图及其他项目文件不受该样例更新影响。

### 10.3 收益记录与完成定义

使用上述固定问题，在相同源码快照、相同输出上限下比较两条路线：A 仅 browse/path/source/preview；B 概念 search/context/traverse 后 preview。每题至少重复 3 次，分别记录冷启动建图与已有图复用；保留原始响应、参数和失败原因。

| 记录项 | 口径 |
| --- | --- |
| 结论正确性 | 当前源码、机制文档和具体测试断言核对；不以命中数量评分 |
| 工具调用量 | 查询、预览、预检、失败重试分别计数 |
| 阅读与输出 | 读取文件数、展示行数、JSON UTF-8 字节数；不要把 indexed_bytes 当作实际读取量 |
| 建图维护成本 | 人工确认的节点/边/依据、初次建图调用、文件变化后的复核调用单列 |
| 路径与截断 | 可复现路径、是否缺失、多父是否保留、是否明确预算不足 |
| 时延 | 作为辅助指标，标注机器与缓存条件；不从单次墙钟推断长期收益 |

实测：六个问题各重复 3 次。语义地图为 8 个节点、23 条关系、31 条依据引用、8 个唯一依据文件。q4 在已有图下用 3 次工具调用得到 fresh 且不需要 preview；依据变化后 4 次调用定位 stale 并预览 1 个文件。q6 两条隔离路线都确认同名节点和相对路径未串项目。q1–q3 在 8 文件 fixture 中，完整展开图关系读取了更多关联文件；q5 无概念命中后正常 fallback。结论是收益集中于新鲜度与项目隔离，不能宣称普遍减少源码读取。方法、逐项指标与限制见 [Affector 试用结果](verification/affector_case_report.md)，完整工具参数和响应见 [原始结果](verification/affector_case_results.json)。

- [x] 所有固定问题的必要事实与文件路由正确，不因历史材料或截断产生错误结论。
- [x] G01–G06 的自动化验收、真实 MCP 入口验证、迁移与导出检查通过，跳过项注明原因。
- [x] 至少一个代表问题在已有图复用时减少无关读取，并完整报告建图成本及没有改善的题目；没有收益时据实记录，不靠加节点宣告成功。
- [x] README 的能力、参数、错误、数据备份/恢复和故障说明与实现一致；更新总任务列表实际勾选状态。

**最小 Read Set：** 本文所有验收标准、上述 ACProgram 阅读入口、`tests/test_semantic_map.py`、`tests/test_mcp_stdio.py`、原阶段 6。

## 11. ACProgram 后续任务移交条件

该部分保留原 Draft 的待裁定项，供未来独立性能任务使用，不在本次 GALHCG 文档编写中修改 ACProgram：

1. 明确“固化”的对象：condition truth、Active Entry 集、派生贡献还是持久化状态；分别说明生命周期与非目标。
2. 按 addLeaf 当前全部 target 建立依赖→事件→状态写入→重建/恢复→测试矩阵；stat 与未知依赖在证据不足时保留轮询。
3. 单独采样每 Tick 条件求值数、事件命中实例数、激活效果次数、派生重算次数和结算耗时；先记录规模与基线，再确定目标阈值。
4. 验证反复翻转、跨实体依赖、卸载、重建和一次性效果不重发；修正文档/注释冲突。
5. `task-0034-affector-performance-review` 等归档只作为历史路由，任何结论重新回到当前实现核查。

## 12. 最终状态

G01–G07 已完成。阶段 5 的新鲜度维护和阶段 4 的图查询扩展均已交付；G07 将收益限制一并记录。ACProgram 的 Affector 运行时任务保持独立，当前目录中的源代码、文档与测试未修改。

## 13. 实施结果与最终契约

- `context` 独立返回邻居、结构父/子、多父祖先、节点依据和每条关系依据的完整性信息。节点及关系的新鲜度为 `fresh / stale / unknown`；每次检查最多 20 个唯一文件、256 MiB 和 15 秒，响应显示本次用量。依据可用 `evidence_offset` 与 `evidence_limit` 续读。
- schema v6 增加 `maps_to.roles`、遍历修订号和节点/关系的新鲜度最近观察记录。`status` 会报告已知 stale、最近观察为 fresh/unknown 的 owner 和未检查 owner；这是最近一次 `context` 观察，可能过时，当前状态须再次查询 `context`。
- `contains` 支持 Concept→Concept 多父 DAG，并统一检测所有 contains 结构环；Concept→Concept 也可使用有向 `depends_on`。`traverse` 默认深度 2、硬上限 8，每页最多 50 个节点与 100 条边，最多访问 1,000 个节点/2,000 条边/2 秒；游标绑定项目、查询条件和地图修订号。
- `search(mode="map")` 支持 `node_types` 与 `matched_fields`；默认仍区分大小写，Agent 工作流显式传 `case_sensitive=false`。`maps_to.roles` 是关联用途，不自动推断文件时序；当前/历史来源仍遵循所选项目的文档规则。
- `resolve_paths` 在指定项目内精确返回规范路径和 File ID。未入清单时提示局部 refresh。`update_map(dry_run=true)` 会用正式提交相同的验证器试写后回滚，聚合最多 50 条独立错误；报告操作数组、引用数、唯一路径、上限和工作成本。错误依赖或预算未完成时不会报告预检通过。
- 数据库从当前 schema v4 迁移至 v6，以及 v1/v2 旧库兼容已验证；迁移失败会回滚。图 Markdown 导出显示关系方向和文件角色。
- 项目身份排查增加 `root_fingerprint`，避免根据截断根目录前缀误判身份。本次没有复现连接/项目身份冲突，因此没有新增服务实例 ID 或连接标识。

GALHCG 全量回归结果、迁移/stdio 覆盖和唯一跳过项见 README 的“开发验证”章节。G07 在 ACProgram 临时副本上完成，唯一已确认能直接减少源码读取的情形是 fresh 依据复用；q1–q3 的完整关系展开在小型 fixture 中读取量更高。该收益不证明完整项目的一般性能提升。旧的 Affector 文件头冲突已在试用报告中指出，但按只读边界未修改；也未运行 ACProgram 测试或修改其状态写入逻辑。
