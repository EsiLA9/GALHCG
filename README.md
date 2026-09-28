# 项目预览 MCP

一个本地 MCP 服务与语义审阅界面，让 Coding Agent 和用户浏览、搜索、预览本地项目，维护 SQLite 文件清单与轻量语义地图。MCP 工具包括 `browse`、`preview`、`search`、`refresh`、`refresh_history`、`status`、`list_files`、`resolve_paths`、`update_map`、`context` 和 `traverse`。索引只保存文件元数据、运行记录、简短语义条目和版本凭据，不保存源码正文；MCP 浏览与刷新不会修改目标文件，显式导出命令可在目标项目内生成报告。

## 环境与安装

需要 Python 3.11 或更新版本。项目已在 Windows 与 Python 3.13.1 上开发验证。服务使用官方 MCP Python SDK 2.2.0 和 pathspec 1.1.1，依赖版本在 `pyproject.toml` 中固定。

在 PowerShell 中进入本项目目录并安装：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
```

### 本地语义审阅界面

安装后可用独立入口启动只读语义地图审阅界面。它与 stdio MCP 入口分开运行，共用项目配置和 SQLite 数据目录；MCP 客户端继续使用 `project-preview-mcp`：

```powershell
& .\.venv\Scripts\project-preview-ui.exe --root 'D:\work\my-project'
```

多项目模式下传入同一组 `--project`、`--data-dir` 和 `--exclude` 参数：

```powershell
& .\.venv\Scripts\project-preview-ui.exe `
  --project 'app=D:\work\app' `
  --project 'tools=D:\work\tools' `
  --data-dir 'D:\codex-data\project-preview'
```

界面只监听本机 loopback 地址并默认打开浏览器。关闭终端中的进程即可停止。可用 `--port` 指定端口，`--no-browser` 禁止自动打开浏览器。关系图支持调整节点间距，以及在完整换行和固定尺寸省略之间切换卡片文字模式。文件预览支持分页、当前文件搜索和尾部预览；若尾部行号无法在预算内确认，界面会明确标记。文件搜索依赖 ripgrep（`rg`）。

点击“重新核验依据”会检查选中 Concept 的依据并更新数据库中该节点最近一次 freshness 观察；浏览、搜索、预览和打开文件不会修改语义地图。关系依据可在检查器中单独核验。界面不提供 `update_map` 或 `refresh` 操作。

调用“系统编辑器”时，默认使用操作系统的文件关联。也可通过 `--editor` 或 `VISUAL` / `EDITOR` 环境变量指定命令；命令以参数数组启动，不经过 shell。命令参数中的 `{path}` 会替换为已校验的项目内绝对路径；未写 `{path}` 时，服务会把路径作为最后一个参数追加。

单项目模式继续使用 `--root`，且无需在工具调用中填写项目 ID：

```powershell
& .\.venv\Scripts\python.exe -m project_preview --root 'D:\work\my-project'
```

需要管理多个项目时，为每个工作目录分配不同且稳定的 ID。ID 长度为 1–64 个 ASCII 字符，首字符为字母或数字，其余字符限于字母、数字、点、下划线和连字符。ID 一旦写入索引数据库就会绑定到该规范化根目录；同一仓库的不同工作目录也要使用不同 ID：

```powershell
& .\.venv\Scripts\python.exe -m project_preview `
  --project 'app=D:\work\app' `
  --project 'tools=D:\work\tools' `
  --data-dir 'D:\codex-data\project-preview'
```

`--root` 与 `--project` 不能同时使用。多项目配置下，访问具体项目的工具需提供 `project_id`；省略时，单项目服务会自动选中项目，多项目服务会返回可用 ID。`status` 不传 ID 时汇总全部已配置项目。`--data-dir` 必须是项目根目录之外的绝对路径；不指定时，Windows 默认 `%LOCALAPPDATA%\project-preview-mcp`，macOS 使用 `~/Library/Application Support/project-preview-mcp`，其他系统使用 `$XDG_DATA_HOME/project-preview-mcp` 或 `~/.local/share/project-preview-mcp`。SQLite 文件名为 `project-preview.sqlite3`，服务启动时会自动建立目录并迁移数据库结构。

## 索引备份与恢复

语义地图包含人工维护的概念、关系与依据版本，属于项目数据，不是可丢弃缓存。备份或恢复前先停止服务，再复制整个 `--data-dir`（默认目录见上文）；恢复后使用同一组项目 ID 和根目录重新启动。服务启动时会向前迁移受支持的旧 schema；新版本数据库不能交给较旧服务打开。若只丢失文件清单，可以运行 `refresh` 重建清单；这不会恢复已删除的语义图。

Windows PowerShell 示例：

```powershell
Copy-Item -LiteralPath "$env:LOCALAPPDATA\project-preview-mcp" `
  -Destination 'D:\backup\project-preview-mcp' -Recurse
```

已安装入口也可直接启动：

```powershell
& .\.venv\Scripts\project-preview-mcp.exe --root 'D:\work\my-project'
```

以上命令启动 stdio MCP 服务，运行期间不会向 stdout 写日志；诊断信息写入 stderr。直接在终端启动后，可按 `Ctrl+C` 停止。

## 导出图存储内容为 Markdown

如需查看已存储的语义图，可运行独立命令读取 SQLite 并导出 Mermaid 关系图、节点摘要、全部关系、证据路径和文件元数据。该命令只读数据库，不启动 MCP，也不导出源码正文、版本令牌或版本密钥。默认读取当前用户的数据目录中 `project-preview.sqlite3` 的 `default` 项目；默认把 `<项目名>-graph.md` 写到项目根目录旁：

```powershell
& .\.venv\Scripts\project-preview-export-map-md.exe --project-id default
```

多项目模式可指定项目 ID，也可以指定服务数据目录和输出路径：

```powershell
& .\.venv\Scripts\project-preview-export-map-md.exe `
  --project-id app `
  --data-dir 'D:\codex-data\project-preview' `
  --output 'D:\exports\app-graph.md'
```

Markdown 的“语义摘要层”显示图中已保存的 Module/Concept 摘要及其字符数；这反映已存储的概括结构，不是自动计算的源码压缩率。

## 索引构建历史

每次 `refresh` 会在服务 SQLite 中持久化一条运行记录：项目 ID、范围、状态、开始/结束时间、扫描耗时、文件/目录数、发现文件的元数据大小总和、移除项数量及失败信息。它不保存源码内容或逐文件正文。可通过 MCP 的 `refresh_history` 分页读取，也可将所选项目的历史导出到项目内的 Markdown：

```powershell
& .\.venv\Scripts\project-preview-export-history-md.exe --project-id app
```

默认报告位置为 `<项目根目录>/.project-preview-mcp/refresh-history.md`。该保留目录由服务默认排除规则跳过，避免日志被下一次 refresh 当作项目源码纳入。可用 `--data-dir` 指定 SQLite 数据目录、用 `--output` 改报告路径（必须仍位于所选项目内）。报告同时列出开始/结束时间差（近似构建总耗时）和单调时钟测得的清单扫描耗时；扫描耗时不包含 SQLite 提交。“元数据字节”是发现的普通文件大小合计，不是服务读取的源码字节数。

## 导出原始项目文本

如需查看磁盘上的项目文本原文，可使用另一个独立导出命令；它不会启动 MCP 服务，也不读取 SQLite。默认将所有遵循项目忽略规则且可读取的 UTF-8 文本文件，按相对路径分节汇总到项目根目录旁的 `<项目名>-contents.md`。二进制、非 UTF-8 或无法完整读取的文件会列入导出报告：

```powershell
& .\.venv\Scripts\project-preview-export-md.exe --root 'D:\work\my-project'
```

也可以指定输出路径：

```powershell
& .\.venv\Scripts\project-preview-export-md.exe `
  --root 'D:\work\my-project' `
  --output 'D:\exports\my-project-contents.md'
```

## MCP 客户端配置

MCP 客户端通常要求重启或重新连接后读取配置。以下 JSON 片段适用于使用 `mcpServers` 配置格式的客户端；将两处路径替换为本机绝对路径：

```json
{
  "mcpServers": {
    "project-preview": {
      "command": "C:\\path\\to\\GALHCG\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "project_preview",
        "--root",
        "D:\\work\\my-project"
      ]
    }
  }
}
```

也可以把 `command` 指向 `.venv\Scripts\project-preview-mcp.exe`，并在 `args` 中仅保留 `--root` 和项目根目录。客户端不需要设置 `cwd`。

多项目配置示例：

```json
{
  "mcpServers": {
    "project-preview": {
      "command": "C:\\path\\to\\GALHCG\\.venv\\Scripts\\python.exe",
      "args": [
        "-m", "project_preview",
        "--project", "app=D:\\work\\app",
        "--project", "tools=D:\\work\\tools",
        "--data-dir", "D:\\codex-data\\project-preview"
      ]
    }
  }
}
```

旧的 `--root` 单项目启动参数仍有效，项目 ID 固定为 `default`。同一数据目录中，ID 不能改绑另一个根目录；如果切换到另一工作目录，请配置一个新 ID。

源码搜索需要单独安装 ripgrep，并确保 `rg` 可从 `PATH` 找到。Windows 可使用以下任一命令安装：

```powershell
winget install BurntSushi.ripgrep.MSVC
# 或
scoop install ripgrep
```

macOS 和其他 Linux 发行版的安装方式见 [ripgrep 官方安装说明](https://github.com/BurntSushi/ripgrep#installation)。安装后可运行 `rg --version` 检查。缺少 ripgrep 时，只有 `search(mode="source")` 不可用，浏览、路径搜索和预览仍可使用。

## 工具

在单项目模式下，`project_id` 可省略（`--root` 对应 ID `default`）。多项目模式下，凡是访问特定项目的工具都要求提供 `project_id`；`status` 可省略 ID 来汇总本次服务配置的项目。

### `browse`

列出目录的直接子项，并按相对路径排序。参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `directory` | `""` | 项目根目录或项目内相对目录，使用 `/` 分隔 |
| `limit` | `100` | 每页数量，最大 `500`；超过上限时按上限返回 |
| `offset` | `0` | 已枚举结果中的起始位置 |

每项包含 `path` 和 `type`（`file` 或 `directory`）。`pagination` 会返回下一页偏移、是否还有结果、枚举是否触顶以及继续浏览提示。单次最多检查 10,000 个直接子项；更大的目录会明确返回 `enumeration_limited`，并提示进入更小的子目录。结果正文最多 48,000 UTF-8 字节。

调用示例：

```json
{"directory":"src","limit":100,"offset":0}
```

### `preview`

从 1 起始的行号读取 UTF-8 文本，接受 UTF-8 BOM，并返回每行的实际行号。参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `path` | 必填 | 项目内相对文件路径，使用 `/` 分隔 |
| `start_line` | `1` | 起始行，必须大于等于 1 |
| `line_count` | `80` | 请求行数，最大 `200`；超过上限时按上限返回 |

调用示例：

```json
{"path":"src/main.py","start_line":1,"line_count":80}
```

常见流程是先将 `browse` 返回的文件 `path` 传给 `preview`，再按 `next_start_line` 读取下一段。

`preview` 还会返回不透明的 `version_token`、`version_token_status` 与 `version_token_reason`。对不超过 64 MiB、且快照在 5 秒预算内完成的文件，服务将同一份内存快照用于预览和生成令牌；令牌只对应实际预览所用的字节。快照内容最多 64 MiB，只保留在内存、不写入磁盘。超大、超时或在快照时检测到变化的文件仍可按原预算预览，但不会获得令牌。令牌用于 `update_map` 依据校验，调用方无需计算或暴露文件哈希。

结果中的 `truncated`、`reason`、`next_start_line` 和 `continuation` 说明内容是否截断及如何继续。正常到达文件末尾时 `reason` 为 `file_end`，且没有下一行号。输出最多 48,000 UTF-8 字节；单行最多 8,192 字节；一次调用最多读取 1 MiB。超长行不会被伪装成完整行，也不支持拆分读取行内字节。读取预算不足以定位请求的行号时，结果会明确说明无法续读，因为原型没有行索引。

错误响应包含 `ok: false`、可区分的 `error` 代码和说明。常见代码包括 `invalid_path`、`not_found`、`not_directory`、`not_file`、`ignored`、`unreadable`、`link_disallowed`、`binary` 和 `unsupported_encoding`。

### `search`

使用 `mode` 明确选择路径搜索、源码搜索或语义地图搜索。路径均为项目根目录下的相对路径；搜索范围和结果会遵循与 `browse`、`preview` 相同的 `.gitignore`、默认排除及链接边界。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `mode` | 必填 | `path` 按相对路径中的文字片段查找文件；`source` 搜索 UTF-8 源码文本；`map` 搜索语义地图 |
| `query` | 必填 | `path`/`source` 最多 512 个字符，并拒绝空值、NUL 和换行；`map` 最多 256 个字符，并拒绝空值和 NUL |
| `directory` | `""` | 限定项目根目录或其子目录，仅适用于 `path`/`source`；`map` 不接受此参数 |
| `limit` | `20` | 返回结果数，最大 `100`；超过时会截断并说明原因 |
| `context_lines` | `0` | 源码命中行前后各返回多少行，范围 `0` 到 `5`；仅适用于 `source` |
| `case_sensitive` | `true` | 是否区分大小写 |
| `offset` | `0` | 仅 `map` 模式使用的分页偏移 |
| `node_types` | 不限类型 | `map` 节点类型筛选：`Project`、`Module`、`Concept`、`File` 的非空子集 |

路径搜索示例：

```json
{"mode":"path","query":"入口","directory":"src","limit":20}
```

源码搜索默认按字面文本匹配，不解析正则表达式。以连字符开头的查询也按普通文字处理：

```json
{"mode":"source","query":"-feature-flag","directory":"src","limit":20,"context_lines":2}
```

源码命中包含 `path`、`line_number`、短 `snippet`、可选 `context` 及片段截断状态。无法按 UTF-8 读取或在搜索期间变得不可访问的命中会列在 `skipped_files`，不会以替换字符伪装成有效文本。将命中的相对路径和行号传给 `preview`，即可继续阅读：

```json
{"path":"src/main.py","start_line":42,"line_count":40}
```

每次搜索最多运行 5 秒、最多枚举 20,000 个目录项，工具结果正文最多 48,000 UTF-8 字节；超长行的命中片段会缩短。`outcome` 为 `complete`、`no_results`、`truncated` 或 `timeout`。缺少 ripgrep 时返回 `error: "rg_unavailable"`，ripgrep 执行错误返回 `error: "search_failed"`。截断结果说明 `reason` 和建议的缩小范围方式。

### `refresh`

建立或更新项目文件元数据清单。它只枚举文件系统项的相对路径、类型、大小和修改时间，不读取或保存源文件内容。首次使用 `list_files` 前需要先刷新：

```json
{"project_id":"app","directory":""}
```

省略 `directory` 或传空字符串会完整刷新项目。指定子目录会先扫描该范围，扫描全部成功后再用一个 SQLite 事务更新清单，并只删除该目录及其子项中已不存在的记录。刷新子目录不会影响其他目录；当子目录已删除时，刷新其仍存在的父目录，或执行完整刷新。扫描遇到不可访问目录、路径超限、条目上限（500,000）或 120 秒时限时，会保留已有清单并记录失败；如果服务在刷新期间中断，`status` 会显示未完成尝试。每条刷新记录还保存扫描耗时、发现文件元数据字节总量和移除项数量，可用 `refresh_history` 分页读取。符号链接、目录联接和特殊文件按服务的访问边界跳过。同一服务进程内，同一项目的刷新会排队串行处理。若多个服务进程同时使用同一数据目录，SQLite 仍保证单次提交原子性，但并发扫描未串行化，最后提交的扫描结果生效。

### `refresh_history`

查询一个已配置项目的持久化刷新记录，按开始时间倒序返回。支持 `limit`（默认 20，最大 100）和 `offset` 分页；每条记录含范围、状态、耗时、文件/目录数、元数据字节、移除项及失败原因。响应不超过 48,000 字节。

```json
{"project_id":"app","limit":20,"offset":0}
```

### `status`

返回每个项目的根目录、文件清单、最近刷新情况及语义地图覆盖摘要。`semantic_map` 统计 `maps_to` 和当前清单内的已存依据路径，不计入服务自动生成的 Project→File 结构边；它表示建立过关联的范围，不代表理解准确率。`freshness` 统计每个 owner 最近一次 `context` 观察到的 stale、fresh、unknown 与未检查数量；这些观察可能已过时，`status` 不会全库哈希。根目录过长时，结果会设置 `root_truncated: true` 并附带 `root_fingerprint`；比较服务配置身份时应使用完整启动配置、项目 ID 与指纹，不要按截断前缀判断。多项目模式下省略 `project_id` 可汇总本次服务配置的项目；状态结果按项目 ID 排序并支持 `limit`、`offset` 翻页。输出受 48,000 字节上限约束。

### `list_files`

按已成功刷新的持久化清单查询，不会读取源码。默认只返回文件；设 `include_directories` 为 `true` 可一并列出目录，便于观察文件与目录路径的类型变化。支持项目内 `directory` 前缀过滤、稳定的路径排序与分页：

```json
{"project_id":"app","directory":"src","limit":100,"offset":0}
```

响应包含 `total`、`next_offset`、路径、类型、大小、修改时间和稳定的 `node_id`，结果正文不超过 48,000 UTF-8 字节。下一页把 `next_offset` 作为新的 `offset`；如果本页因字节预算缩短，会设置 `output_limited: true`、`reason: "output_budget"`。`list_files` 显示最近一次成功刷新的快照；刷新前的新增、删除和重命名不会自动反映在其中。`preview` 始终读取磁盘当前内容。

### `update_map`、`context` 与语义地图搜索

阶段 4 增加轻量语义地图，不会自动分析或总结源码。服务自动维护 `Project` 与 `File` 节点；Agent 通过 `update_map` 管理 `Module` 与 `Concept` 节点。语义节点 ID 必须稳定且分别以 `module:`、`concept:` 开头，长度最多 128 个安全 ASCII 字符。同名节点允许并存，按 `project_id` 和 ID 消歧。名称最多 120 字符，短摘要最多 500 字符，最多 20 个别名（每项最多 120 字符）。`tentative` 可不带依据；`confirmed` 至少需要一个依据。

关系方向及节点类型固定如下：

| 关系 | 允许方向 |
| --- | --- |
| `contains` | Project → Module/File；Module → Module/Concept/File；Concept → Concept（允许多父 DAG，结构关系整体无环） |
| `maps_to` | Concept → File |
| `depends_on` | Module → Module；Concept → Concept（有向依赖允许成环） |
| `related_to` | Concept ↔ Concept（无向关系，只存一条） |

`update_map` 接受 `upsert_nodes`、`delete_node_ids`、`upsert_edges`、`delete_edges`、`dry_run` 和 `project_id`。节点对象字段为 `id`、`type`、`name`、`summary`、`aliases`、`state`、`evidence`；边对象字段为 `source_id`、`relation`、`target_id`、`evidence`，`maps_to` 可另带 `roles`（`implementation`、`documentation`、`test` 中的一项或多项；旧关系显示为 `unspecified`）。每条依据严格使用 `{"path":"相对路径","version_token":"preview 返回的令牌"}`。可以省略空数组；每次最多 upsert 50 个节点、删除 50 个节点、upsert 100 条边、删除 100 条边并校验 32 条依据引用。同一路径被两个 owner 引用按两条引用计算；同一 owner 内重复引用无效。整个批次先校验端点、类型、重复关系、`contains` 环和当前文件版本，再以一个事务提交；任一项失败时整批不写入。

`dry_run: true` 使用同一校验流程检查端点、结构、依据令牌、预算和预计操作数量，然后回滚试写事务，不留下图变更或可复用的批准票据。预检会尽量聚合独立错误，最多返回 50 条；`errors_complete: false` 表示因依赖或预算原因仍有项目未检查。正式提交会重新检查文件版本、图结构和端点。Project/File 节点由服务维护，不能手动创建或删除；删除语义节点会级联删除其关系和依据。响应最多 48,000 字节。

如果需要把工作分成多个 `update_map` 调用，每次成功提交都独立生效；后续批次失败不会回滚之前成功的批次。记录已成功写入的 ID，并在失败后从剩余概念继续。

先预览实际依据文件并保存其返回的 `version_token`，再把该令牌随节点或关系一起提交。文件在预览后变化时，服务拒绝整批更新并列出需要重新读取的文件。令牌是不透明值；服务不返回原始内容哈希。

一个典型写入流程是先用 `preview` 取得依据令牌、用 `resolve_paths` 按完整相对路径取得文件节点 ID，再预检并提交确认概念与文件关系：

```json
{"project_id":"app","paths":["src/auth.py"]}
```

若路径存在但未进入清单，结果会建议刷新对应目录；服务不会为查 ID 而自动扫描整个项目。

```json
{
  "project_id": "app",
  "upsert_nodes": [
    {
      "id": "concept:session-auth",
      "type": "Concept",
      "name": "Session authentication",
      "summary": "Validates a session token before granting access.",
      "aliases": ["session verification"],
      "state": "confirmed",
      "evidence": [{"path": "src/auth.py", "version_token": "<preview.version_token>"}]
    }
  ],
  "upsert_edges": [
    {
      "source_id": "concept:session-auth",
      "relation": "maps_to",
      "target_id": "<resolve_paths.results[0].node_id>",
      "roles": ["implementation"],
      "evidence": [{"path": "src/auth.py", "version_token": "<preview.version_token>"}]
    }
  ]
}
```

`search(mode="map", query=...)` 按节点名称、别名和短摘要检索，使用 `offset` 与 `limit` 分页，可设 `node_types: ["Concept"]` 限定类型。结果含 `matched_fields`，说明命中名称、摘要还是别名。由于默认区分大小写，Agent 搜索概念时应显式传 `case_sensitive: false`；未找到概念时再转到 `path`/`source` 搜索，不能把普通文件命中当作已建语义概念：

```json
{"mode":"map","query":"authentication","case_sensitive":false,"node_types":["Concept"],"limit":20,"offset":0}
```

```json
{"project_id":"app","node_id":"concept:session-auth","neighbor_limit":10}
```

`context` 返回节点摘要、独立查询的 `contains` 父子结构、多父祖先、相关文件、依据和一跳邻居；`related_to` 会标为无向并保留关系原方向。每个区段都有 `completeness`，包含实际总数、是否完整、截断原因和可继续方式。邻居最多 20 个；依据可用 `evidence_offset`/`evidence_limit` 续读；输出不超过 48,000 字节。节点与关系分别带 `fresh`、`stale` 或 `unknown` 新鲜度及原因。`fresh` 只表示本次检查版本匹配，不保证语义绝对正确；超预算、读取失败或无依据返回 `unknown`。`context` 会保存 owner 最近一次新鲜度观察供 `status` 摘要使用。清单刷新不会清除已观察到的 `stale`；需要 Agent 重新阅读并通过 `update_map` 更新摘要和依据版本。

`traverse` 按关系方向做有界多跳查询，默认从起点沿 `contains` 向外走 2 条边；横向关系须显式加入 `relations`。可用 `node_types` 限制遍历中纳入的节点类型，适合排除服务自动维护的 Project→File 清单边。最大深度为 8，每页最多 50 个节点与 100 条边。结果保留每条边的原始 `source_id`、`target_id`，目标查询返回一条按稳定排序选出的最短路径。`complete` 与 `stop_reason` 说明图遍历是否触及深度、访问量或时间预算；未找到目标仅表示本次范围内未找到。存在 `next_cursor` 时，把游标原样传回以续读。游标绑定项目、查询条件（包括 `node_types`）和地图修订号，图变化或项目不符会要求重新开始。

`maps_to.roles` 描述文件用途，不表示它是当前或历史版本。Agent 应先读取所选项目的 `AGENTS.md` 和当前文档路由，再区分现行实现与历史材料；来源时序无法确认时，保留为未知，不按目录编号或时间自动推断。没有概念命中也不妨碍继续用 `path`/`source` 搜索和 `preview` 阅读。

## 忽略规则与访问边界

服务使用 pathspec 的 Git 忽略规则实现，读取项目根目录及目标路径祖先目录下的 `.gitignore`。嵌套规则和否定规则遵循目录层级；已被忽略的父目录不能通过子目录中的否定规则重新开放。默认排除 `.git`、`.hg`、`.svn`、`.project-preview-mcp`、`node_modules`、Python 虚拟环境与缓存、常见构建目录及覆盖率输出。`.project-preview-mcp/` 是服务生成报告的保留目录。

可在启动命令中重复添加项目相对的额外排除模式；这些模式不能通过 `.gitignore` 的否定规则取消：

```powershell
& .\.venv\Scripts\python.exe -m project_preview `
  --root 'D:\work\my-project' `
  --exclude 'fixtures/private/' `
  --exclude '*.generated'
```

所有工具共用同一套边界：拒绝绝对路径、越界路径、ADS 冒号、符号链接与目录联接；访问前解析真实路径并应用忽略规则。Windows 下也会拒绝尾空格、尾点、设备名和无法对应到规范路径的短文件名别名。浏览会跳过链接与特殊文件，不会跟随链接形成循环。`.gitignore` 文件最多读取 256,000 字节。

预览只读取常规文件；SQLite 数据库位于服务数据目录，不会写入目标项目根目录。索引含路径和文件元数据，不含源码副本。二进制内容和非 UTF-8 编码会返回不同错误。

## 开发验证

安装后可运行内置回归测试：

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m unittest tests.test_semantic_map tests.test_mcp_stdio -v
& .\.venv\Scripts\python.exe verification\accept_stage1.py
```

完整回归共运行 58 项：57 项通过，1 项 Windows 符号链接用例因缺少创建权限跳过；其中语义地图专项 21 项、SDK stdio MCP 测试 2 项。独立阶段 1 协议验收 11 项通过，2 项符号链接检查因相同权限跳过。回归还覆盖持久化、多项目隔离、工作目录绑定、清单分页、局部刷新、文件/目录类型变化、刷新历史统计和分页、失败原子性和未完成刷新状态。

语义地图测试覆盖 schema v1–v6 迁移、早期 v2 证据表兼容和当前 v4 升级、重启持久化、节点/边新鲜度、批量原子性、概念多父结构、遍历游标与预算、文件角色导出、过期令牌、文件删除级联和输出预算；真实 SDK stdio 验收会调用 `update_map`、`context`、`search(mode="map")`、`resolve_paths` 和 `traverse`。

测试覆盖浏览与分页、路径与源码搜索、中文内容、忽略规则、路径边界、预算截断、文件错误、链接限制，以及通过官方 SDK 客户端进行的真实 stdio MCP 连接。

Affector 语义图试用可在有 ACProgram 源码目录时运行；它会建立临时源码副本和临时数据库，不改写 ACProgram：

```powershell
& .\.venv\Scripts\python.exe verification\accept_affector_case.py `
  --acprogram-root 'D:\work\ACProgram'
```

逐项调用参数和原始响应保存在 `verification/affector_case_results.json`，简要结论见 `verification/affector_case_report.md`。

第二条命令运行独立 MCP 协议验收客户端。详细结果、修复记录与环境限制见 [阶段1验收记录](阶段1验收记录.md)。48,000 字节上限针对工具结果 JSON 正文，MCP 协议包装和文本/结构化内容副本另有传输开销。
