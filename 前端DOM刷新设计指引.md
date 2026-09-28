# 前端 DOM 刷新设计指引

> 本指引供 GALHCG 本地查询前端设计与实现时参考。内容摘录并适配自同级 ACProgram 的 [`frontend-design-project-overlay.md`](../ACProgram/docs/ai/frontend-design-project-overlay.md) 第 1–5 节。
> 原协议针对 ACProgram 游戏内 UI；本指引沿用其中的 DOM 刷新与交互连续性约束，不规定本地查询前端的配色、字体或整体视觉风格。

## 1. 视觉设计与 DOM 更新分离

视觉签名优先通过既有节点、CSS token、pseudo-element、稳定宿主或 CSS variable 实现。新增 wrapper 必须承担真实的布局、语义、交互或可访问性职责；纯装饰 wrapper 默认不允许。

装饰线、切角、噪点、渐变、角标和氛围层，先尝试 CSS 或现有表现宿主；只有无法满足语义、布局或可访问性要求时才新增节点。独特设计不以增加 DOM 为默认手段。

## 2. 刷新等级是硬边界

每个界面方案必须先声明刷新等级：

- **behavior**：只修改已有节点的文字、class、属性或 CSS variable；适用于高频或局部状态变化。
- **region**：只替换明确的面板、结果列表、详情区或元素组；适用于内容集合不变但输出结构变化的场景。
- **structure**：才允许重建应用根节点或整个工作区；仅适用于页面集合、路由、工作区归属或语义节点归属发生变化。

滚动、悬停、聚焦、普通点击和单项选择默认只能触发 behavior 或 region，不得直接调用全量 `render()`。批量状态变更必须在提交完成后合并到单一 UI 收敛点。

## 3. 设计计划必须包含 DOM budget

开始实现前，设计说明必须回答：

```text
DOM budget:
- 新增 wrapper / 节点数量：
- 可复用的稳定节点：
- behavior patch 范围：
- region 替换范围：
- 允许 full render 的唯一条件：

Interaction preservation:
- scroll 位置如何保留：
- focus 如何保留：
- hover / popover 如何保留或关闭：
- click 后哪些节点必须保持 identity：
```

如果无法给出稳定节点、刷新范围和降级条件，方案还不能作为可施工设计。

## 4. 交互连续性不通过重建恢复

- scroll 只操作滚动容器自身，不通过父级 render 间接恢复位置。
- 切换页签或选择项只更新目标面板；未变化的列表和导航保持节点 identity。
- 展开 / 折叠优先切换 class、`hidden` 或属性，不重建父树。
- 事件委托优先绑定到稳定父节点，避免每次刷新重新注册整棵树。
- DOM 替换必须经过统一的 mutation range 生命周期：替换前关闭受影响范围内的 popover，替换后校验锚点仍连接当前 DOM；未受影响范围的 hover 不得被误伤。

## 5. 验收要求

界面改动至少应能证明：

- 普通状态更新不触发 full render；
- scroll / click 不重建无关节点；
- 局部替换不会残留锚点已断开的 tooltip / popover；
- 批量提交不会为每个 mutation 重复刷新；
- 新增结构数量和刷新次数符合设计说明。

优先使用项目已有的刷新诊断属性、刷新观察记录、调用链分析和对应 UI 回归测试。需要视觉或交互证据时补充真实浏览器验收；源码调用次数不能代替 DOM 与实际交互证据。若本地查询前端尚无同类诊断能力，则在实现时选用等效的观测和验收方式，不假定 ACProgram 的工具已存在于本项目。
