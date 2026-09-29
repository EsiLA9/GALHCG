(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="local-ui-token"]')?.content || "";
  const GRAPH_CARD_WIDTH = 220;
  const GRAPH_SPACING = {
    roomy: { horizontal: 84, vertical: 44 },
    spacious: { horizontal: 144, vertical: 76 },
    wide: { horizontal: 216, vertical: 112 },
  };
  const GRAPH_PREFERENCES_KEY = "project-preview-ui.graph-preferences.v1";
  const state = {
    projects: [], project: null, projectId: null, rootId: null,
    treeCache: new Map(), expanded: new Set(), treeSearch: "", searchSequence: 0,
    searchResults: [], searchNextOffset: null, searchTotal: 0, searchLoading: false, searchReason: null,
    selectedNode: null, selectedNodeId: null, context: null,
    graphMode: "structure", graphData: { nodes: new Map(), edges: new Map() }, graphCursor: null,
    moduleFilesCursor: null, moduleFilesResult: null,
    graphResult: null, mappings: { nodes: new Map(), edges: new Map() }, mappingCursor: null,
    graphSpacing: "roomy", graphCardMode: "wrap", graphDepth: 1,
    graphRelations: ["depends_on", "related_to"], graphDirection: "both", graphFocusPending: false,
    sourcePath: null, sourceMeta: null, preview: null, previewNextLine: null, previewMode: "page",
    contextSequence: 0, graphSequence: 0, mappingSequence: 0, sourceSequence: 0,
    previewSequence: 0, fileSearchSequence: 0,
    cardMappingCache: new Map(), cardPreviewEpoch: 0, cardPreviewSequence: 0,
    activeCardPreview: null, cardPreviewCloseTimer: null, suppressCardPreviewFocus: false,
    changeSequence: 0, changeOwnerSequence: 0, changeEntries: [], changeNextOffset: null,
    changeResponse: null, changeLoading: false,
    toastTimer: null,
  };

  class ApiError extends Error {
    constructor(payload) {
      super(payload?.message || "本地查询失败。");
      this.code = payload?.error || "request_failed";
      this.payload = payload;
    }
  }

  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new ApiError(payload);
    return payload;
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Local-UI-Token": token,
        Accept: "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new ApiError(payload);
    return payload;
  }

  function el(tag, className = "", text = null) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null && text !== undefined) node.textContent = String(text);
    return node;
  }

  function makeButton(text, className, handler, attrs = {}) {
    const button = el("button", className, text);
    button.type = "button";
    for (const [key, value] of Object.entries(attrs)) button.setAttribute(key, value);
    button.addEventListener("click", handler);
    return button;
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
  }

  function ensureCardPreviewHost() {
    let host = $("graph-file-preview");
    if (host) return host;
    host = el("div", "graph-file-preview");
    host.id = "graph-file-preview";
    host.hidden = true;
    host.setAttribute("role", "dialog");
    host.setAttribute("aria-modal", "false");
    host.setAttribute("aria-label", "Concept 关联文件预览");
    host.setAttribute("aria-describedby", "graph-file-preview-note");
    document.body.append(host);
    host.addEventListener("pointerenter", () => clearTimeout(state.cardPreviewCloseTimer));
    host.addEventListener("pointerleave", scheduleCardPreviewClose);
    host.addEventListener("focusout", (event) => {
      if (!host.contains(event.relatedTarget)) scheduleCardPreviewClose();
    });
    return host;
  }

  function cardPreviewCacheKey(projectId, nodeId) {
    return `${projectId}\u0000${nodeId}`;
  }

  function closeCardFilePreview({ restoreFocus = false } = {}) {
    clearTimeout(state.cardPreviewCloseTimer);
    const active = state.activeCardPreview;
    if (active?.anchor?.isConnected) active.anchor.setAttribute("aria-expanded", "false");
    state.activeCardPreview = null;
    const host = $("graph-file-preview");
    if (host) host.hidden = true;
    if (restoreFocus && active?.anchor?.isConnected) {
      state.suppressCardPreviewFocus = true;
      active.anchor.focus();
      state.suppressCardPreviewFocus = false;
    }
  }

  function invalidateCardFilePreviewCache() {
    state.cardPreviewEpoch += 1;
    state.cardMappingCache.clear();
    closeCardFilePreview();
  }

  function scheduleCardPreviewClose() {
    clearTimeout(state.cardPreviewCloseTimer);
    state.cardPreviewCloseTimer = setTimeout(() => {
      const active = state.activeCardPreview;
      const host = $("graph-file-preview");
      if (!active) return;
      const pointerInside = active.anchor?.matches(":hover") || host?.matches(":hover");
      const focusInside = document.activeElement === active.anchor || host?.contains(document.activeElement);
      if (!pointerInside && !focusInside) closeCardFilePreview();
    }, 160);
  }

  function positionCardFilePreview() {
    const active = state.activeCardPreview;
    const host = $("graph-file-preview");
    if (!active || !host || host.hidden) return;
    if (!active.anchor?.isConnected) {
      closeCardFilePreview();
      return;
    }
    const rect = active.anchor.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      closeCardFilePreview();
      return;
    }
    const margin = 12;
    const width = Math.min(360, Math.max(240, window.innerWidth - margin * 2));
    const height = Math.min(host.scrollHeight || 240, window.innerHeight - margin * 2);
    const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
    let top = rect.bottom + 8;
    if (top + height > window.innerHeight - margin) top = rect.top - height - 8;
    top = Math.max(margin, Math.min(top, window.innerHeight - height - margin));
    host.style.left = `${left}px`;
    host.style.top = `${top}px`;
  }

  function cardMappingNeedsAttention(result) {
    return Boolean(result?.next_cursor || result?.output_limited
      || (result?.stop_reason && result.stop_reason !== "depth_limit"));
  }

  function renderCardFilePreview(record) {
    const host = ensureCardPreviewHost();
    const active = state.activeCardPreview;
    if (!active) return;
    host.replaceChildren();
    host.hidden = false;

    const heading = el("div", "graph-file-preview-head");
    const titleGroup = el("div", "graph-file-preview-title");
    titleGroup.append(
      el("strong", "", active.node.name || active.node.node_id),
      el("span", "", "关联文件 · maps_to"),
    );
    const closeButton = makeButton("×", "graph-file-preview-close", () => closeCardFilePreview({ restoreFocus: true }), {
      "aria-label": "关闭关联文件预览",
    });
    heading.append(titleGroup, closeButton);
    host.append(heading);
    const note = el("p", "graph-file-preview-note", "文件映射用于导航，不代表摘要的文件级依据。");
    note.id = "graph-file-preview-note";
    host.append(note);

    if (record?.status === "loading" || !record) {
      host.append(el("div", "graph-file-preview-message", "正在读取关联文件…"));
      positionCardFilePreview();
      return;
    }
    if (record.status === "error") {
      host.append(el("div", "graph-file-preview-message is-error", `文件关联暂不可用：${record.message}`));
      host.append(makeButton("重试读取", "graph-file-preview-action", async () => {
        const key = cardPreviewCacheKey(state.projectId, active.node.node_id);
        state.cardMappingCache.delete(key);
        await showCardFilePreview(active.node, active.anchor, { forceReload: true });
      }));
      positionCardFilePreview();
      return;
    }

    const result = record.result;
    const nodes = new Map((result.nodes || []).map((node) => [node.node_id, node]));
    const edges = (result.edges || []).filter((edge) => edge.relation === "maps_to"
      && edge.source_id === active.node.node_id);
    const total = Number.isInteger(result.total_edges) ? result.total_edges : edges.length;
    const incomplete = cardMappingNeedsAttention(result);
    const traversalCut = Boolean(result.stop_reason && result.stop_reason !== "depth_limit");
    const visibleEdges = edges.slice(0, 3);
    for (const edge of visibleEdges) {
      const file = nodes.get(edge.target_id);
      if (!file) continue;
      const path = file.path || file.name || file.node_id;
      const row = makeButton("", "graph-file-preview-row", async () => {
        const projectId = state.projectId;
        closeCardFilePreview();
        await openSource(path, {});
        if (projectId === state.projectId && state.sourcePath === path) $("source-path").focus();
      }, { "aria-label": `打开文件 ${path}，角色 ${edge.roles?.join("、") || "unspecified"}` });
      const role = el("span", "graph-file-preview-role", (edge.roles?.length ? edge.roles : ["unspecified"]).join(" · "));
      row.append(el("code", "graph-file-preview-path", path), role);
      host.append(row);
    }

    if (!total && incomplete) {
      host.append(el("div", "graph-file-preview-message", "当前查询没有读到关联文件，结果不完整，暂不能确认此 Concept 没有关联。"));
    } else if (!total) {
      host.append(el("div", "graph-file-preview-message", "此 Concept 尚无关联文件。"));
    } else if (traversalCut) {
      host.append(el("div", "graph-file-preview-message is-warning", `已读取至少 ${total} 项关联；查询达到预算，未能确认完整数量。`));
    } else if (incomplete) {
      host.append(el("div", "graph-file-preview-message is-warning", `共发现 ${total} 项；当前页之后仍有结果。`));
    } else if (total > visibleEdges.length) {
      host.append(el("div", "graph-file-preview-message", `共发现 ${total} 项 · 当前显示 ${visibleEdges.length} 项`));
    }

    if (total > visibleEdges.length || incomplete) {
      const count = traversalCut ? `至少 ${total}` : total;
      host.append(makeButton(`查看全部 ${count} 个文件 →`, "graph-file-preview-action", async () => {
        const node = active.node;
        closeCardFilePreview();
        await openAllConceptFiles(node);
      }));
    }
    positionCardFilePreview();
  }

  async function fetchCardMappings(node) {
    const projectId = state.projectId;
    const nodeId = node.node_id;
    const epoch = state.cardPreviewEpoch;
    const key = cardPreviewCacheKey(projectId, nodeId);
    const record = { status: "loading", result: null, message: "", promise: null };
    state.cardMappingCache.set(key, record);
    record.promise = postJson("/api/traverse", {
      project_id: projectId,
      start_node_id: nodeId,
      relations: ["maps_to"],
      direction: "outgoing",
      max_depth: 1,
      node_limit: 50,
      edge_limit: 100,
      cursor: null,
      node_types: ["Concept", "File"],
    }).then((result) => {
      if (epoch !== state.cardPreviewEpoch || projectId !== state.projectId) return null;
      record.status = "ready";
      record.result = result;
      record.promise = null;
      return record;
    }).catch((error) => {
      if (epoch !== state.cardPreviewEpoch || projectId !== state.projectId) return null;
      record.status = "error";
      record.message = friendlyError(error);
      record.promise = null;
      return record;
    });
    return record.promise;
  }

  async function showCardFilePreview(node, anchor, { forceReload = false } = {}) {
    if (!node || node.type !== "Concept" || !node.node_id || !anchor?.isConnected) return;
    clearTimeout(state.cardPreviewCloseTimer);
    const prior = state.activeCardPreview;
    if (prior?.anchor?.isConnected && prior.anchor !== anchor) prior.anchor.setAttribute("aria-expanded", "false");
    const active = {
      node,
      anchor,
      projectId: state.projectId,
      sequence: ++state.cardPreviewSequence,
    };
    state.activeCardPreview = active;
    anchor.setAttribute("aria-expanded", "true");
    const host = ensureCardPreviewHost();
    host.hidden = false;
    host.setAttribute("aria-label", `${node.name || "Concept"} 的关联文件预览`);

    const key = cardPreviewCacheKey(state.projectId, node.node_id);
    if (forceReload) state.cardMappingCache.delete(key);
    let record = state.cardMappingCache.get(key);
    if (!record && state.selectedNodeId === node.node_id && state.mappingResult) {
      record = {
        status: "ready",
        result: {
          ...state.mappingResult,
          nodes: [...state.mappings.nodes.values()],
          edges: [...state.mappings.edges.values()],
          next_cursor: state.mappingCursor,
        },
      };
      state.cardMappingCache.set(key, record);
    }
    renderCardFilePreview(record || { status: "loading" });
    if (record?.status === "ready" || record?.status === "error") return;
    if (record?.promise) {
      await record.promise;
    } else {
      await fetchCardMappings(node);
    }
    const current = state.activeCardPreview;
    if (!current || current.sequence !== active.sequence || current.projectId !== state.projectId) return;
    renderCardFilePreview(state.cardMappingCache.get(key));
  }

  async function openAllConceptFiles(node) {
    if (!node || node.type !== "Concept") return;
    if (state.selectedNodeId !== node.node_id) await selectNode({ id: node.node_id, ...node });
    if (state.selectedNodeId !== node.node_id) return;
    state.graphMode = "files";
    updateGraphTabs();
    await loadGraph(true);
    $("view-files").focus();
  }

  function friendlyError(error) {
    if (error instanceof ApiError) {
      if (error.code === "stale_cursor" || error.code === "stale_revision") return "关系图已变化，请重新载入当前范围。";
      if (error.code === "rg_unavailable") return "文件内搜索需要安装 ripgrep；源码预览仍可使用。";
      return error.message;
    }
    return "本地查询失败，请确认界面服务仍在运行。";
  }

  function formatDate(value) {
    if (!value) return "未记录";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function formatMtime(value) {
    if (value === null || value === undefined) return "清单中无记录";
    const date = new Date(Number(value) / 1_000_000);
    return Number.isNaN(date.getTime()) ? "清单中无记录" : formatDate(date.toISOString());
  }

  function changeStatusLabel(status) {
    return ({
      content_changed: "内容已变化",
      metadata_only: "仅元数据变化",
      metadata_since_refresh: "清单后有变化",
      missing: "文件缺失",
      unknown: "暂时无法判断",
      unchanged: "内容未变化",
      no_evidence: "无内容依据",
    })[status] || status || "未知状态";
  }

  function changeReasonText(reason) {
    return ({
      not_found: "文件不存在",
      unavailable: "无法读取文件元数据",
      unreadable: "文件不可读取",
      ignored: "文件受忽略规则排除",
      link_disallowed: "文件路径包含被拒绝的链接",
      not_regular_file: "目标不是普通文件",
      byte_budget: "达到本次读取字节预算",
      time_budget: "达到本次核验时间预算",
      hash_timeout: "文件版本核验超时",
      file_too_large: "文件超过版本核验大小上限",
      file_budget: "文件超过版本核验大小上限",
      metadata_unavailable: "无法读取文件修改时间",
      content_changed: "当前内容与依据记录版本不同",
    })[reason] || (reason ? String(reason).replaceAll("_", " ") : "检查预算或文件访问受限");
  }

  function changeStatusNote(entry) {
    const notes = {
      content_changed: "有 " + (entry.stale_evidence_reference_count || 0) + " 条依据的内容令牌与当前文件不同，建议复核对应概念或关系。",
      metadata_only: "文件的修改时间或大小变化，但当前内容与已记录依据相同；不表示摘要过期。",
      metadata_since_refresh: "文件元数据自上次清单扫描后变化。此文件没有 Evidence 版本基线，内容是否改变尚未确认。",
      missing: "文件当前不存在或路径已失效；关联内容需要复核。",
      unknown: "本次无法完成内容比对：" + changeReasonText(entry.reason) + "。",
      unchanged: entry.time_baseline_missing_count
        ? "内容令牌匹配；旧记录没有捕获时修改时间，无法提供精确的 mtime 对照。"
        : "内容令牌与记录版本相同。",
      no_evidence: "只有文件导航关系，没有保存该文件版本的 Evidence；不能据此判断语义内容是否变化。",
    };
    return notes[entry.status] || "当前状态需要人工查看。";
  }

  function renderChangeEntry(entry) {
    const card = el("article", "change-entry " + entry.status);
    const head = el("div", "change-entry-head");
    const pathButton = makeButton(entry.path, "change-path", () => {
      $("changes-dialog").close();
      openSource(entry.path, {
        mtime_ns: entry.current_mtime_ns,
        indexed_at: entry.manifest_indexed_at,
        size: entry.current_size,
        source_mtime: true,
      });
    });
    head.append(pathButton, el("span", "change-status-pill " + entry.status, changeStatusLabel(entry.status)));
    card.append(head);

    const captured = (entry.owners || []).filter((owner) => owner.owner_type !== "maps_to"
      && owner.captured_mtime_ns !== null && owner.captured_mtime_ns !== undefined);
    const uniqueCapturedTimes = [...new Set(captured.map((owner) => owner.captured_mtime_ns))];
    const baselineValue = entry.evidence_reference_count
      ? (uniqueCapturedTimes.length === 1
        ? formatMtime(uniqueCapturedTimes[0])
        : uniqueCapturedTimes.length > 1 ? "本页依据有多个版本" : "旧记录无捕获时间")
      : formatMtime(entry.manifest_mtime_ns);
    const pair = el("div", "change-time-pair");
    const current = el("dl", "change-time");
    current.append(el("dt", "", "文件当前修改时间"), el("dd", "", formatMtime(entry.current_mtime_ns)));
    const link = el("span", "change-time-link", "↔");
    link.setAttribute("aria-hidden", "true");
    const baseline = el("dl", "change-time");
    baseline.append(
      el("dt", "", entry.evidence_reference_count ? "依据版本捕获 mtime（当前页）" : "上次清单 mtime"),
      el("dd", "", baselineValue),
    );
    pair.append(current, link, baseline);
    card.append(pair, el("p", "change-entry-note" + (entry.needs_attention ? " warning" : ""), changeStatusNote(entry)));
    card.append(el("div", "change-owner-heading",
      "直接关联 · Evidence " + entry.evidence_reference_count + " · maps_to " + entry.maps_to_count));

    const ownerList = el("div", "change-owner-list");
    const owners = entry.owners || [];
    for (const owner of owners) {
      const row = el("div", "change-owner");
      const copy = el("div", "change-owner-copy");
      const isMapping = owner.owner_type === "maps_to";
      const ownerKind = isMapping
        ? "导航映射 · " + (owner.direction || "Concept → File")
        : owner.owner_type === "edge"
          ? "关系依据 · " + (owner.relation || "关系") + " · " + (owner.direction || "source → target")
          : "节点依据 · " + (owner.node_type || "Concept");
      copy.append(el("span", "", owner.name || owner.owner_id), el("small", "", ownerKind));
      if (!isMapping) {
        const detail = [
          owner.captured_mtime_ns !== null && owner.captured_mtime_ns !== undefined
            ? "捕获 mtime " + formatMtime(owner.captured_mtime_ns) : "捕获 mtime 不可用",
          "依据记录 " + formatDate(owner.evidence_created_at),
        ].join(" · ");
        copy.append(el("small", "", detail));
      } else if (owner.roles && owner.roles.length) {
        copy.append(el("small", "", "角色 " + owner.roles.join(" · ")));
      }
      row.append(copy);
      row.append(el("span", "change-owner-status " + (owner.status || ""),
        isMapping ? "仅导航" : changeStatusLabel(owner.status)));

      const actions = el("div", "change-owner-actions");
      const conceptId = owner.owner_type === "node" ? owner.owner_id : owner.source_id;
      const conceptType = owner.node_type;
      if (conceptId && ["Concept", "Module"].includes(conceptType)) {
        actions.append(makeButton("查看概念", "", async () => {
          const selected = {
            id: conceptId, node_id: conceptId, type: conceptType,
            name: owner.owner_type === "node" ? owner.name : owner.source_name, summary: "",
          };
          $("changes-dialog").close();
          await selectNode(selected);
        }));
      }
      if (!isMapping && ["node", "edge"].includes(owner.owner_type)) {
        actions.append(makeButton("重新核验并记录", "", async () => {
          const sequence = state.changeSequence;
          const projectId = state.projectId;
          await verifyOwner(owner.owner_type, owner.owner_id);
          if (sequence === state.changeSequence && projectId === state.projectId && $("changes-dialog").open) {
            await loadChangePath(entry.path, 0, false);
          }
        }));
      }
      if (actions.childElementCount) row.append(actions);
      ownerList.append(row);
    }
    card.append(ownerList);
    if (!owners.length) card.append(el("p", "change-entry-note", "当前没有可显示的直接关联项。"));
    if (entry.owner_next_offset !== null && entry.owner_next_offset !== undefined) {
      card.append(makeButton("继续查看直接关联（已显示 " + owners.length + " / " + entry.owner_total + "）", "text-button", () => {
        loadMoreChangeOwners(entry);
      }));
    }
    return card;
  }

  function renderChanges() {
    const host = $("changes-list");
    host.replaceChildren();
    const response = state.changeResponse;
    if (!response) return;
    for (const entry of state.changeEntries) host.append(renderChangeEntry(entry));
    const summary = $("changes-summary");
    summary.hidden = false;
    summary.textContent = "范围 " + (response.scope || ".") + " · 已检查 " + response.checked_paths
      + " / " + response.total_paths + " 个直接关联文件 · 本次读取 "
      + formatBytes(response.work?.bytes_hashed || 0) + " · 检查于 " + formatDate(response.checked_at);
    const status = $("changes-status");
    status.className = "changes-status" + (response.complete ? "" : " warning");
    if (!state.changeEntries.length && response.checked_paths) {
      status.textContent = response.next_offset !== null
        ? "本页没有发现需要注意的文件；仍有后续文件可检查。"
        : response.complete ? "检查完成；没有发现需要注意的文件。"
          : "检查未完整：" + (response.stop_reason || "结果受限") + "。可继续检查或缩小目录范围。";
    } else if (response.stop_reason) {
      status.textContent = "本次检查未完整：" + response.stop_reason + "。已明确标出已检查范围，可继续或缩小目录范围。";
    } else {
      status.textContent = response.complete
        ? "检查完成。未变化文件默认不显示，但已计入检查数量。"
        : "当前仅显示这一页；后续文件尚未检查。";
    }
    $("changes-page-note").textContent = response.complete
      ? "只覆盖所选项目与目录内有直接图关联的文件。"
      : "当前页 " + response.offset + " 起；结果范围有限，停止原因：" + (response.stop_reason || "尚有下一页") + "。";
    $("changes-next").hidden = response.next_offset === null || response.next_offset === undefined;
  }

  async function loadChangesPage(offset = 0, { append = false } = {}) {
    if (!state.projectId) return;
    const sequence = ++state.changeSequence;
    const projectId = state.projectId;
    state.changeLoading = true;
    $("changes-run").disabled = true;
    $("changes-next").disabled = true;
    $("changes-status").className = "changes-status";
    $("changes-status").textContent = append ? "继续检查下一页…" : "正在检查直接引用的文件…";
    if (!append) {
      state.changeEntries = [];
      state.changeResponse = null;
      state.changeScannedPaths = 0;
      $("changes-list").replaceChildren();
      $("changes-summary").hidden = true;
      $("changes-next").hidden = true;
    }
    try {
      const response = await postJson("/api/changes", {
        project_id: projectId,
        directory: $("changes-directory").value.trim(),
        limit: 10,
        offset,
        include_unchanged: $("changes-include-unchanged").checked,
      });
      if (sequence !== state.changeSequence || projectId !== state.projectId) return;
      state.changeScannedPaths = (state.changeScannedPaths || 0) + response.checked_paths;
      state.changeResponse = { ...response, checked_paths: state.changeScannedPaths };
      state.changeNextOffset = response.next_offset;
      state.changeEntries = append ? [...state.changeEntries, ...(response.entries || [])] : (response.entries || []);
      state.changeLoading = false;
      renderChanges();
    } catch (error) {
      if (sequence !== state.changeSequence || projectId !== state.projectId) return;
      state.changeResponse = null;
      state.changeLoading = false;
      $("changes-status").className = "changes-status warning";
      $("changes-status").textContent = friendlyError(error);
      if (!append) $("changes-list").replaceChildren();
      $("changes-next").hidden = true;
    } finally {
      if (sequence === state.changeSequence) {
        $("changes-run").disabled = false;
        $("changes-next").disabled = false;
      }
    }
  }

  async function loadChangePath(path, ownerOffset = 0, appendOwners = false) {
    const projectId = state.projectId;
    const sequence = ++state.changeOwnerSequence;
    try {
      const response = await postJson("/api/changes", {
        project_id: projectId, path, owner_offset: ownerOffset, owner_limit: 5, include_unchanged: true,
      });
      if (sequence !== state.changeOwnerSequence || projectId !== state.projectId) return;
      const updated = response.entries?.[0];
      if (!updated) return;
      const index = state.changeEntries.findIndex((entry) => entry.path === path);
      if (index >= 0) {
        const previous = state.changeEntries[index];
        state.changeEntries[index] = {
          ...updated,
          owners: appendOwners ? [...(previous.owners || []), ...(updated.owners || [])] : updated.owners,
        };
      } else {
        state.changeEntries.push(updated);
      }
      renderChanges();
    } catch (error) {
      showToast(friendlyError(error), true);
    }
  }

  async function loadMoreChangeOwners(entry) {
    await loadChangePath(entry.path, entry.owners?.length || 0, true);
  }

  function formatBytes(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function freshnessLabel(freshness) {
    if (!freshness) return { text: "未核验", className: "unknown" };
    if (freshness.status === "fresh") return { text: "最近核验通过", className: "fresh" };
    if (freshness.status === "stale") return { text: "发现依据变化", className: "stale" };
    if (freshness.reason === "not_checked") return { text: "尚未核验", className: "unknown" };
    if (freshness.reason === "no_evidence") return { text: "无文件依据", className: "unknown" };
    return { text: "结果不完整", className: "unknown" };
  }

  function freshnessReasonText(reason) {
    return ({
      file_count_budget: "达到文件数量预算",
      byte_budget: "达到读取字节预算",
      time_budget: "达到核验时间预算",
      evidence_count_limit: "依据数量超过单次核验范围",
      check_incomplete: "部分依据未能确认",
      not_checked: "尚未核验",
      no_evidence: "没有文件级依据",
      missing_file: "依据文件不存在",
      unreadable: "依据文件无法读取",
      ignored: "依据文件当前受忽略规则排除",
      not_regular_file: "依据目标不是普通文件",
      content_changed: "文件内容与记录版本不同",
    })[reason] || (reason ? String(reason).replaceAll("_", " ") : "");
  }

  function freshnessExplanation(freshness) {
    if (!freshness) return "";
    const perFile = [...new Set((freshness.files || []).map((item) => freshnessReasonText(item.reason)).filter(Boolean))];
    const reasons = perFile.length ? perFile : [freshnessReasonText(freshness.reason)].filter(Boolean);
    return reasons.slice(0, 2).join("；");
  }

  function scopeReason(value) {
    const reasons = {
      depth_limit: "已到当前深度上限",
      node_budget: "已达到节点访问预算",
      edge_budget: "已达到关系访问预算",
      time_budget: "已达到查询时间预算",
      visit_budget: "已达到访问预算",
      result_limit: "结果数量受限",
      output_budget: "结果达到输出预算",
      neighbor_limit: "邻居结果受限",
    };
    return reasons[value] || value || "结果完整";
  }

  function statItem(label, value) {
    const wrap = el("div");
    const displayValue = value === null || value === undefined || value === "" ? "—" : value;
    wrap.append(el("dt", "", label), el("dd", "", displayValue));
    return wrap;
  }

  function setProjectMeta(project) {
    const meta = $("project-meta");
    const last = project?.last_successful_refresh || project?.latest_refresh;
    const pieces = [];
    if (project) pieces.push(`${project.discovered_file_count || 0} 个文件`);
    if (last?.finished_at) pieces.push(`最近清单 ${formatDate(last.finished_at)}`);
    if (project?.refresh_incomplete) pieces.push("存在未完成刷新");
    $("project-meta-text").textContent = pieces.join(" · ") || "等待选择项目";
    meta.classList.toggle("is-warning", Boolean(project?.refresh_incomplete));
    const coverage = project?.semantic_map;
    $("concept-count").textContent = coverage?.semantic_node_count ?? "—";
  }

  function updateSelectionHeader(node) {
    if (!node) return;
    $("selected-title").textContent = node.type === "Project" ? node.name : (node.name || node.path || node.id);
    $("selected-summary").textContent = node.type === "Project"
      ? (state.project?.root || "当前项目的概念结构")
      : (node.summary || node.path || `${nodeTypeLabel(node.type)} · 选择后查看局部关系`);
  }

  async function loadProjects() {
    const select = $("project-select");
    try {
      state.projects = [];
      let offset = 0;
      let pages = 0;
      while (pages < 100) {
        const response = await getJson(`/api/status?limit=50&offset=${offset}`);
        state.projects.push(...(response.projects || []));
        pages += 1;
        if (response.next_offset === null || response.next_offset === undefined || response.next_offset <= offset) break;
        offset = response.next_offset;
      }
      select.replaceChildren();
      if (!state.projects.length) {
        select.add(new Option("没有配置项目", ""));
        select.disabled = true;
        showToast("没有可供此界面查看的已配置项目。", true);
        return;
      }
      for (const project of state.projects) {
        const suffix = project.root_truncated ? ` · ${project.root_fingerprint}` : "";
        select.add(new Option(`${project.project_id}${suffix}`, project.project_id));
      }
      select.disabled = false;
      select.addEventListener("change", () => switchProject(select.value));
      $("concept-search").disabled = false;
      $("tree-reload").disabled = false;
      $("tree-refresh").disabled = false;
      $("changes-open").disabled = false;
      await switchProject(state.projects[0].project_id);
    } catch (error) {
      $("project-meta-text").textContent = friendlyError(error);
      showToast(friendlyError(error), true);
    }
  }

  async function switchProject(projectId) {
    const project = state.projects.find((item) => item.project_id === projectId);
    if (!project) return;
    if ($("changes-dialog").open) $("changes-dialog").close();
    state.changeSequence += 1;
    state.changeOwnerSequence += 1;
    state.changeEntries = [];
    state.changeResponse = null;
    state.changeNextOffset = null;
    invalidateCardFilePreviewCache();
    state.projectId = projectId;
    state.contextSequence += 1;
    state.graphSequence += 1;
    state.mappingSequence += 1;
    state.sourceSequence += 1;
    state.previewSequence += 1;
    state.fileSearchSequence += 1;
    state.searchSequence += 1;
    state.searchLoading = false;
    state.project = project;
    state.rootId = project.root_node_id;
    state.treeCache.clear();
    state.expanded = new Set([state.rootId]);
    state.treeSearch = "";
    state.selectedNode = { id: state.rootId, type: "Project", name: projectId, summary: "" };
    state.selectedNodeId = state.rootId;
    state.context = null;
    state.graphMode = "structure";
    state.graphDepth = 1;
    state.graphData = { nodes: new Map(), edges: new Map() };
    state.graphCursor = null;
    state.moduleFilesCursor = null;
    state.moduleFilesResult = null;
    state.graphResult = null;
    state.mappings = { nodes: new Map(), edges: new Map() };
    state.mappingCursor = null;
    state.mappingResult = null;
    state.sourcePath = null;
    state.sourceMeta = null;
    state.preview = null;
    $("project-select").value = projectId;
    $("concept-search").value = "";
    setProjectMeta(project);
    updateSelectionHeader(state.selectedNode);
    $("footer-scope").textContent = "结果仅覆盖当前查询范围";
    renderTree();
    renderInspector();
    updateGraphTabs();
    await Promise.allSettled([
      fetchTreeChildren(state.rootId, true),
      loadGraph(true).catch((error) => showToast(friendlyError(error), true)),
    ]);
  }

  function nodeTypeLabel(type) {
    return ({ Project: "PROJECT", Module: "MODULE", Concept: "CONCEPT", File: "FILE" })[type] || String(type || "NODE").toUpperCase();
  }

  function renderTree() {
    const nav = $("concept-tree");
    nav.replaceChildren();
    if (!state.project) return;
    if (state.treeSearch) {
      renderSearchResults(nav);
      return;
    }
    const root = {
      id: state.rootId, type: "Project", name: state.project.project_id,
      summary: state.project.root || "", state: null,
    };
    const list = el("ul", "tree-list");
    list.append(renderTreeEntry(root, new Set()));
    nav.append(list);
  }

  function collectLoadedParentCounts() {
    const counts = new Map();
    for (const [parentId, branch] of state.treeCache.entries()) {
      for (const edge of branch.edges.values()) {
        if (edge.relation === "contains" && edge.source_id === parentId) {
          counts.set(edge.target_id, (counts.get(edge.target_id) || 0) + 1);
        }
      }
    }
    return counts;
  }

  function renderTreeEntry(node, ancestors) {
    const li = el("li");
    li.dataset.nodeId = node.id;
    li.treeNode = node;
    li.treeAncestors = new Set(ancestors);
    const row = el("div", `tree-row${state.selectedNodeId === node.id ? " selected" : ""}`);
    const branch = state.treeCache.get(node.id);
    const canExpand = ["Project", "Module", "Concept"].includes(node.type);
    const expanded = state.expanded.has(node.id);
    const toggle = makeButton(branch?.loading ? "·" : (expanded ? "⌄" : "›"), `tree-toggle${canExpand ? "" : " empty"}`, async (event) => {
      event.stopPropagation();
      if (!canExpand) return;
      if (state.expanded.has(node.id)) {
        state.expanded.delete(node.id);
        updateTreeBranch(li);
        return;
      }
      state.expanded.add(node.id);
      updateTreeBranch(li);
      if (!state.treeCache.has(node.id)) await fetchTreeChildren(node.id, true);
    }, { "aria-expanded": canExpand ? String(expanded) : "false", "aria-label": expanded ? "收起" : "展开" });
    const select = makeButton("", "tree-select", async () => {
      await selectNode(node.id ? node : { id: node.node_id, ...node });
    });
    select.dataset.nodeId = node.id;
    const symbol = el("span", `node-symbol ${String(node.type).toLowerCase()}`);
    const name = el("span", "tree-name", node.name || node.id);
    select.append(symbol, name);
    const parentCounts = collectLoadedParentCounts();
    if ((parentCounts.get(node.id) || 0) > 1) select.append(el("span", "shared-mark", "共享"));
    row.append(toggle, select);
    li.append(row);
    appendTreeBranch(li);
    return li;
  }

  function appendTreeBranch(li) {
    const node = li.treeNode;
    if (!node || !state.expanded.has(node.id) || !["Project", "Module", "Concept"].includes(node.type)) return;
    const branch = state.treeCache.get(node.id);
    const children = branch ? getDirectChildren(node.id, branch) : [];
    if (!children.length && branch && branch.complete) {
      const message = node.id === state.rootId && !(state.project?.discovered_file_count)
        ? "还没有文件清单。点“扫描文件”即可建立；概念与关系由 ChatGPT 生成。"
        : node.id === state.rootId && !(state.project?.semantic_map?.semantic_node_count)
          ? "尚无概念与关系。请在 ChatGPT 中用 project-preview-mcp 生成；若已有内容，请检查两种入口是否使用同一 --data-dir。"
          : "此概念没有下级节点。";
      li.append(el("div", "tree-message", message));
    } else if (!children.length && !branch) {
      li.append(el("div", "tree-message", "展开以读取下级节点。"));
    }
    if (children.length) {
      const childList = el("ul", "tree-list");
      const nextAncestors = new Set(li.treeAncestors || []);
      nextAncestors.add(node.id);
      for (const child of children) {
        if (nextAncestors.has(child.id)) continue;
        childList.append(renderTreeEntry(child, nextAncestors));
      }
      li.append(childList);
    }
    if (branch?.error) {
      li.append(el("div", "tree-message", branch.error));
      li.append(makeButton("重新读取分支", "text-button tree-load-more", async () => fetchTreeChildren(node.id, true)));
      return;
    }
    if (branch?.loading) {
      li.append(el("div", "tree-message", children.length ? "继续读取子项…" : "读取直接子项…"));
      return;
    }
    if (branch?.nextCursor) {
      li.append(makeButton("继续载入子项 →", "text-button tree-load-more", async () => fetchTreeChildren(node.id, false)));
    } else if (branch && !branch.complete) {
      li.append(el("div", "tree-message", `子项结果不完整：${scopeReason(branch.stopReason)}`));
    }
  }

  function updateTreeBranch(li) {
    const node = li.treeNode;
    const toggle = li.querySelector(":scope > .tree-row > .tree-toggle");
    const canExpand = ["Project", "Module", "Concept"].includes(node?.type);
    const expanded = Boolean(node && state.expanded.has(node.id));
    if (toggle) {
      toggle.textContent = state.treeCache.get(node?.id)?.loading ? "·" : (expanded ? "⌄" : "›");
      toggle.setAttribute("aria-expanded", canExpand ? String(expanded) : "false");
      toggle.setAttribute("aria-label", expanded ? "收起" : "展开");
    }
    for (const child of [...li.children].slice(1)) child.remove();
    appendTreeBranch(li);
  }

  function updateTreeBranchById(nodeId) {
    const li = [...$("concept-tree").querySelectorAll("li[data-node-id]")]
      .find((item) => item.dataset.nodeId === nodeId);
    if (li) updateTreeBranch(li);
  }

  function updateSelectedTreeRows() {
    for (const row of $("concept-tree").querySelectorAll(".tree-row")) {
      const select = row.querySelector(".tree-select");
      row.classList.toggle("selected", select?.dataset.nodeId === state.selectedNodeId);
    }
  }

  function updateSharedTreeMarks() {
    const parentCounts = collectLoadedParentCounts();
    for (const select of $("concept-tree").querySelectorAll(".tree-select[data-node-id]")) {
      const hasMultipleParents = (parentCounts.get(select.dataset.nodeId) || 0) > 1;
      const existing = select.querySelector(".shared-mark");
      if (hasMultipleParents && !existing) select.append(el("span", "shared-mark", "共享"));
      if (!hasMultipleParents && existing) existing.remove();
    }
  }

  function getDirectChildren(parentId, branch) {
    const ids = new Set();
    for (const edge of branch.edges.values()) {
      if (edge.relation === "contains" && edge.source_id === parentId) ids.add(edge.target_id);
    }
    return [...ids].map((id) => {
      const node = branch.nodes.get(id);
      return node ? { id: node.node_id, ...node } : null;
    }).filter(Boolean)
      .sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh-CN") || a.id.localeCompare(b.id));
  }

  async function fetchTreeChildren(parentId, reset) {
    const previous = reset ? null : state.treeCache.get(parentId);
    if (previous?.loading || (!reset && previous && !previous.nextCursor)) return;
    const branch = reset || !previous ? {
      nodes: new Map(), edges: new Map(), loading: true, complete: false,
      nextCursor: null, stopReason: null, revision: null, error: null,
    } : previous;
    const projectId = state.projectId;
    if (reset || !previous) state.treeCache.set(parentId, branch);
    branch.loading = true;
    branch.error = null;
    updateTreeBranchById(parentId);
    try {
      const response = await postJson("/api/traverse", {
        project_id: projectId,
        start_node_id: parentId,
        relations: ["contains"],
        direction: "outgoing",
        max_depth: 1,
        node_limit: 50,
        edge_limit: 100,
        cursor: reset ? null : branch.nextCursor,
        node_types: ["Project", "Module", "Concept"],
      });
      if (projectId !== state.projectId || state.treeCache.get(parentId) !== branch) return;
      for (const node of response.nodes || []) branch.nodes.set(node.node_id, node);
      for (const edge of response.edges || []) branch.edges.set(edge.edge_id, edge);
      branch.nextCursor = response.next_cursor;
      branch.stopReason = response.stop_reason;
      branch.revision = response.revision;
      branch.loading = false;
      const budgetStop = ["node_budget", "edge_budget", "time_budget"].includes(response.stop_reason);
      branch.complete = !response.next_cursor && !budgetStop;
      if (response.stop_reason === "stale_cursor" || response.restart_required) {
        branch.error = "地图已变化，请重新展开此分支。";
        branch.complete = false;
        branch.nextCursor = null;
      }
      updateTreeBranchById(parentId);
      updateSharedTreeMarks();
    } catch (error) {
      if (projectId !== state.projectId || state.treeCache.get(parentId) !== branch) return;
      branch.loading = false;
      branch.error = friendlyError(error);
      updateTreeBranchById(parentId);
    }
  }

  function renderSearchResults(container) {
    const query = state.treeSearch;
    if (!query) return;
    if (state.searchLoading && !state.searchResults.length) {
      container.append(el("div", "tree-message", `搜索“${query}”…`));
      return;
    }
    if (!state.searchResults.length) {
      container.append(el("div", "tree-message", "语义地图未命中此概念。可以缩短查询词或展开项目结构。"));
      return;
    }
    const list = el("ul", "tree-list");
    for (const result of state.searchResults) {
      const li = el("li");
      const row = el("div", `tree-row${state.selectedNodeId === result.id ? " selected" : ""}`);
      const spacer = el("span", "tree-toggle empty");
      const select = makeButton("", "tree-select", () => selectNode(result));
      select.dataset.nodeId = result.id;
      select.append(el("span", `node-symbol ${String(result.type).toLowerCase()}`), el("span", "tree-name", result.name));
      const matched = (result.matched_fields || []).map((field) => ({ name: "名称", summary: "摘要", aliases: "别名" }[field] || field)).join(" · ");
      row.append(spacer, select);
      li.append(row, el("div", "tree-message", `${nodeTypeLabel(result.type)} · 命中${matched ? ` ${matched}` : ""}`));
      list.append(li);
    }
    container.append(list);
    if (state.searchNextOffset !== null) {
      container.append(makeButton(state.searchLoading ? "继续读取搜索结果…" : `继续载入 · 已显示 ${state.searchResults.length} / ${state.searchTotal}`, "text-button tree-load-more", fetchMoreConceptSearch));
    } else if (state.searchReason) {
      container.append(el("div", "tree-message", `搜索结果不完整：${scopeReason(state.searchReason)}`));
    }
  }

  async function runConceptSearch(query) {
    state.treeSearch = query.trim();
    const sequence = ++state.searchSequence;
    const projectId = state.projectId;
    state.searchResults = [];
    state.searchNextOffset = null;
    state.searchTotal = 0;
    state.searchReason = null;
    if (!state.treeSearch) {
      renderTree();
      return;
    }
    state.searchLoading = true;
    renderTree();
    try {
      const response = await postJson("/api/search-map", {
        project_id: projectId, query: state.treeSearch,
        limit: 50, offset: 0, node_types: ["Concept", "Module"],
      });
      if (sequence !== state.searchSequence || projectId !== state.projectId) return;
      state.searchResults = response.results || [];
      state.searchNextOffset = response.next_offset;
      state.searchTotal = response.total || 0;
      state.searchReason = response.truncated && response.next_offset === null ? response.reason : null;
      state.searchLoading = false;
      renderTree();
    } catch (error) {
      if (sequence !== state.searchSequence || projectId !== state.projectId) return;
      state.searchLoading = false;
      state.searchReason = friendlyError(error);
      const nav = $("concept-tree");
      nav.replaceChildren(el("div", "tree-message", friendlyError(error)));
    }
  }

  async function fetchMoreConceptSearch() {
    if (state.searchNextOffset === null || state.searchLoading) return;
    const sequence = state.searchSequence;
    const projectId = state.projectId;
    state.searchLoading = true;
    renderTree();
    try {
      const response = await postJson("/api/search-map", {
        project_id: projectId, query: state.treeSearch,
        limit: 50, offset: state.searchNextOffset, node_types: ["Concept", "Module"],
      });
      if (sequence !== state.searchSequence || projectId !== state.projectId) return;
      state.searchResults.push(...(response.results || []));
      state.searchNextOffset = response.next_offset;
      state.searchTotal = response.total || state.searchTotal;
      state.searchReason = response.truncated && response.next_offset === null ? response.reason : null;
      state.searchLoading = false;
      renderTree();
    } catch (error) {
      if (sequence !== state.searchSequence || projectId !== state.projectId) return;
      state.searchLoading = false;
      state.searchReason = friendlyError(error);
      renderTree();
    }
  }

  async function selectNode(node) {
    if (!node || !node.id) return;
    const changed = state.selectedNodeId !== node.id;
    if (changed) state.graphDepth = 1;
    state.contextSequence += 1;
    state.graphSequence += 1;
    state.mappingSequence += 1;
    state.selectedNode = node;
    state.selectedNodeId = node.id;
    updateSelectionHeader(node);
    state.context = null;
    state.graphData = { nodes: new Map(), edges: new Map() };
    state.graphCursor = null;
    state.moduleFilesCursor = null;
    state.moduleFilesResult = null;
    state.graphResult = null;
    state.mappings = { nodes: new Map(), edges: new Map() };
    state.mappingCursor = null;
    state.mappingResult = null;
    if (changed) {
      state.sourceSequence += 1;
      state.previewSequence += 1;
      state.fileSearchSequence += 1;
      state.sourcePath = null;
      state.sourceMeta = null;
      state.preview = null;
      state.previewNextLine = null;
      $("verify-button").classList.remove("busy");
      $("verify-button").lastElementChild.textContent = "重新核验依据";
    }
    $("footer-scope").textContent = "结果仅覆盖当前查询范围";
    const wasSearching = Boolean(state.treeSearch);
    if (wasSearching) {
      state.treeSearch = "";
      $("concept-search").value = "";
    }
    if (wasSearching) renderTree();
    else updateSelectedTreeRows();
    updateGraphTabs();
    renderGraphEmpty("正在读取局部关系…");
    renderInspector();
    const tasks = [loadGraph(true).catch((error) => showToast(friendlyError(error), true))];
    if (node.type === "Concept" || node.type === "Module") {
      tasks.push(loadContext().catch((error) => showToast(friendlyError(error), true)));
    }
    if (node.type === "File") {
      tasks.push(loadContext().then(async () => {
        const metadata = state.context?.files?.find((file) => file.path === node.path) || {};
        await openSource(node.path, metadata);
      }).catch((error) => showToast(friendlyError(error), true)));
    }
    if (node.type === "Concept") tasks.push(loadMappings(true));
    await Promise.allSettled(tasks);
  }

  function updateGraphTabs() {
    const canShowRelations = ["Concept", "Module"].includes(state.selectedNode?.type);
    const canMapFiles = state.selectedNode?.type === "Concept";
    $("view-files").disabled = !canMapFiles;
    $("view-relations").disabled = !canShowRelations;
    if (!canMapFiles && state.graphMode === "files") state.graphMode = "structure";
    if (!canShowRelations && state.graphMode === "relations") state.graphMode = "structure";
    for (const button of [$("view-hierarchy"), $("view-relations"), $("view-files")]) {
      const active = button.dataset.view === state.graphMode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    }
    $("relation-controls").hidden = state.graphMode !== "relations";
    $("graph-depth-toggle").hidden = state.graphMode !== "structure";
    $("graph-depth-toggle").textContent = state.graphDepth === 1 ? "展开两层" : "收起到一层";
    const total = state.project?.semantic_map?.semantic_edge_count;
    const scopeParts = state.graphMode === "files"
      ? ["maps_to", "1 hop"]
      : state.graphMode === "relations"
        ? [state.graphRelations.map(relationName).join("+ ") || "未选择关系", directionName(state.graphDirection), "1 hop"]
        : ["contains", "双向", `${state.graphDepth} hop`];
    scopeParts.push(`全库 ${total ?? "—"} 边`);
    $("graph-scope").textContent = scopeParts.join(" · ");
    const legendHost = $("graph-legend-lines");
    legendHost.replaceChildren();
    const legendItems = state.graphMode === "files"
      ? ["maps_to"]
      : state.graphMode === "relations"
        ? (state.graphRelations.length ? state.graphRelations : ["depends_on", "related_to"])
        : ["contains"];
    for (const relation of legendItems) {
      const item = el("span", "legend-item");
      item.append(el("i", `legend-line ${relationClass(relation)}`), document.createTextNode(relationName(relation)));
      legendHost.append(item);
    }
  }

  function relationName(relation) {
    return ({ contains: "结构", depends_on: "依赖", related_to: "相关", maps_to: "文件映射" })[relation] || relation;
  }

  function relationClass(relation) {
    return ({ contains: "contains", depends_on: "dependency", related_to: "related", maps_to: "mapping" })[relation] || "contains";
  }

  function directionName(direction) {
    return ({ both: "双向", outgoing: "当前节点指向", incoming: "指向当前节点" })[direction] || direction;
  }

  function selectedGraphRelations() {
    return [...state.graphRelations];
  }

  function updateGraphRelationFilters() {
    state.graphRelations = [];
    if ($("relation-depends").checked) state.graphRelations.push("depends_on");
    if ($("relation-related").checked) state.graphRelations.push("related_to");
    if ($("relation-contains").checked) state.graphRelations.push("contains");
    state.graphDirection = $("graph-direction").value;
    updateGraphTabs();
  }

  async function loadGraph(reset) {
    if (!state.projectId || !state.selectedNodeId) return;
    if (reset) invalidateCardFilePreviewCache();
    const sequence = ++state.graphSequence;
    const projectId = state.projectId;
    const nodeId = state.selectedNodeId;
    const mode = state.graphMode;
    const relations = mode === "files" ? ["maps_to"]
      : mode === "relations" ? selectedGraphRelations() : ["contains"];
    const direction = mode === "relations" ? state.graphDirection
      : mode === "files" ? "outgoing" : "both";
    const maxDepth = mode === "structure" ? state.graphDepth : 1;
    const nodeTypes = mode === "files" ? ["Concept", "File"]
      : mode === "relations"
        ? (state.selectedNode?.type === "Module" && relations.includes("contains")
          ? ["Module", "Concept", "File"] : ["Module", "Concept"])
        : ["Project", "Module", "Concept"];
    if (reset) {
      state.graphData = { nodes: new Map(), edges: new Map() };
      state.graphCursor = null;
      state.moduleFilesCursor = null;
      state.moduleFilesResult = null;
      state.graphResult = null;
      state.graphFocusPending = true;
    }
    const isCurrent = () => sequence === state.graphSequence && projectId === state.projectId
      && nodeId === state.selectedNodeId && mode === state.graphMode;
    const primaryPending = reset || Boolean(state.graphCursor);
    if (primaryPending) {
      const response = await postJson("/api/traverse", {
        project_id: projectId,
        start_node_id: nodeId,
        relations,
        direction,
        max_depth: maxDepth,
        node_limit: 50,
        edge_limit: 100,
        cursor: reset ? null : state.graphCursor,
        node_types: nodeTypes,
      });
      if (!isCurrent()) return;
      for (const node of response.nodes || []) state.graphData.nodes.set(node.node_id, node);
      for (const edge of response.edges || []) state.graphData.edges.set(edge.edge_id, edge);
      state.graphCursor = response.next_cursor;
      state.graphResult = response;
    }

    const shouldLoadModuleFiles = mode === "structure" && state.selectedNode?.type === "Module"
      && (reset || (!primaryPending && state.moduleFilesCursor));
    if (shouldLoadModuleFiles) {
      const response = await postJson("/api/traverse", {
        project_id: projectId,
        start_node_id: nodeId,
        relations: ["contains"],
        direction: "outgoing",
        max_depth: 1,
        node_limit: 50,
        edge_limit: 100,
        cursor: reset ? null : state.moduleFilesCursor,
        node_types: ["Module", "File"],
      });
      if (!isCurrent()) return;
      for (const node of response.nodes || []) state.graphData.nodes.set(node.node_id, node);
      for (const edge of response.edges || []) state.graphData.edges.set(edge.edge_id, edge);
      state.moduleFilesCursor = response.next_cursor;
      state.moduleFilesResult = response;
    }
    if (!primaryPending && !shouldLoadModuleFiles) return;
    renderGraph();
  }

  async function loadMappings(reset) {
    if (!state.selectedNodeId || state.selectedNode?.type !== "Concept") return;
    const sequence = ++state.mappingSequence;
    const projectId = state.projectId;
    const nodeId = state.selectedNodeId;
    if (reset) {
      state.mappings = { nodes: new Map(), edges: new Map() };
      state.mappingCursor = null;
    }
    try {
      const response = await postJson("/api/traverse", {
        project_id: projectId, start_node_id: nodeId,
        relations: ["maps_to"], direction: "outgoing", max_depth: 1,
        node_limit: 50, edge_limit: 100, cursor: reset ? null : state.mappingCursor,
        node_types: ["Concept", "File"],
      });
      if (sequence !== state.mappingSequence || projectId !== state.projectId || nodeId !== state.selectedNodeId) return;
      for (const node of response.nodes || []) state.mappings.nodes.set(node.node_id, node);
      for (const edge of response.edges || []) state.mappings.edges.set(edge.edge_id, edge);
      state.mappingCursor = response.next_cursor;
      state.mappingResult = response;
      renderInspector();
      if (state.graphMode === "files") renderGraph();
    } catch (error) {
      showToast(friendlyError(error), true);
    }
  }

  function renderGraphEmpty(message) {
    $("graph-nodes").replaceChildren();
    $("graph-links").replaceChildren();
    const empty = el("div", "graph-empty");
    empty.append(el("div", "map-glyph"), el("strong", "", message), el("p", "", "查询结果只覆盖当前节点与关系范围。"));
    $("graph-nodes").append(empty);
    $("graph-completeness").textContent = message;
    $("graph-more").hidden = true;
  }

  function wrappedLineCount(value, maxWidth, fontSize) {
    let total = 0;
    for (const paragraph of String(value ?? "").split(/\r?\n/)) {
      let lines = 1;
      let lineWidth = 0;
      for (const character of paragraph) {
        const code = character.codePointAt(0) || 0;
        const glyphWidth = code > 0x2e7f ? fontSize : fontSize * 0.58;
        if (lineWidth > 0 && lineWidth + glyphWidth > maxWidth) {
          lines += 1;
          lineWidth = 0;
        }
        lineWidth += glyphWidth;
      }
      total += lines;
    }
    return Math.max(1, total);
  }

  function estimateCardHeight(node, roleMap) {
    if (state.graphCardMode === "fixed") return 132;
    const title = node.name || node.path || node.node_id;
    const subtitle = node.type === "File" ? (node.path || "") : (node.summary || "");
    const contentWidth = GRAPH_CARD_WIDTH - 28;
    const titleLines = wrappedLineCount(title, contentWidth, 12);
    const subtitleLines = subtitle ? wrappedLineCount(subtitle, contentWidth, 10) : 0;
    const roleHeight = roleMap.get(node.node_id)?.length ? 20 : 0;
    return Math.max(86, 22 + 16 + titleLines * 18 + (subtitle ? 5 + subtitleLines * 15 : 0) + roleHeight);
  }

  function graphLayout(nodes, edges, mode, viewportWidth, roleMap) {
    const nodeMap = new Map(nodes.map((node) => [node.node_id, node]));
    if (state.selectedNode && !nodeMap.has(state.selectedNode.id)) {
      nodeMap.set(state.selectedNode.id, {
        node_id: state.selectedNode.id, type: state.selectedNode.type,
        name: state.selectedNode.name, summary: state.selectedNode.summary || "",
        path: state.selectedNode.path || null, state: state.selectedNode.state || null,
      });
    }
    const positions = new Map();
    const spacing = GRAPH_SPACING[state.graphSpacing] || GRAPH_SPACING.roomy;
    const cardStep = GRAPH_CARD_WIDTH + spacing.horizontal;
    const margin = GRAPH_CARD_WIDTH / 2 + 32;
    const cardHeights = new Map([...nodeMap.values()].map((node) => [node.node_id, estimateCardHeight(node, roleMap)]));
    if (mode === "files") {
      const selected = nodeMap.get(state.selectedNodeId);
      const files = [...nodeMap.values()].filter((node) => node.node_id !== state.selectedNodeId)
        .sort((a, b) => (a.path || a.name).localeCompare(b.path || b.name, "zh-CN"));
      const fileHeight = files.reduce((sum, node) => sum + cardHeights.get(node.node_id), 0)
        + Math.max(0, files.length - 1) * spacing.vertical;
      const height = Math.max(390, fileHeight + 72, cardHeights.get(selected?.node_id) + 72 || 390);
      const selectedX = margin;
      const fileX = selectedX + cardStep;
      if (selected) positions.set(selected.node_id, { x: selectedX, y: height / 2 });
      let y = (height - fileHeight) / 2;
      for (const node of files) {
        const cardHeight = cardHeights.get(node.node_id);
        positions.set(node.node_id, { x: fileX, y: y + cardHeight / 2 });
        y += cardHeight + spacing.vertical;
      }
      const width = Math.max(viewportWidth, fileX + GRAPH_CARD_WIDTH / 2 + margin);
      return { nodeMap, positions, width, height };
    }

    const adjacency = new Map();
    for (const edge of edges) {
      if (mode === "structure" && edge.relation !== "contains") continue;
      if (mode === "relations" && !state.graphRelations.includes(edge.relation)) continue;
      if (!adjacency.has(edge.source_id)) adjacency.set(edge.source_id, []);
      if (!adjacency.has(edge.target_id)) adjacency.set(edge.target_id, []);
      adjacency.get(edge.source_id).push({ id: edge.target_id, side: 1 });
      adjacency.get(edge.target_id).push({ id: edge.source_id, side: -1 });
    }
    const depth = new Map([[state.selectedNodeId, { distance: 0, side: 0 }]]);
    const queue = [state.selectedNodeId];
    while (queue.length) {
      const current = queue.shift();
      const currentValue = depth.get(current);
      for (const next of adjacency.get(current) || []) {
        if (depth.has(next.id)) continue;
        depth.set(next.id, {
          distance: currentValue.distance + 1,
          side: currentValue.side === 0 ? next.side : currentValue.side,
        });
        queue.push(next.id);
      }
    }
    for (const node of nodeMap.values()) {
      if (!depth.has(node.node_id)) depth.set(node.node_id, { distance: 1, side: 1 });
    }
    const groups = new Map();
    for (const [id, value] of depth.entries()) {
      if (!nodeMap.has(id)) continue;
      const key = value.side === 0 ? "center" : `${value.side}:${value.distance}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(nodeMap.get(id));
    }
    const leftDepth = Math.max(0, ...[...depth.values()].filter((value) => value.side < 0).map((value) => value.distance));
    const rightDepth = Math.max(0, ...[...depth.values()].filter((value) => value.side > 0).map((value) => value.distance));
    const center = margin + leftDepth * cardStep;
    let height = 390;
    for (const [key, group] of groups.entries()) {
      if (key === "center") continue;
      group.sort((a, b) => (a?.name || "").localeCompare(b?.name || "", "zh-CN"));
      const groupHeight = group.reduce((sum, node) => sum + cardHeights.get(node.node_id), 0)
        + Math.max(0, group.length - 1) * spacing.vertical;
      height = Math.max(height, groupHeight + 72);
    }
    for (const [key, group] of groups.entries()) {
      if (key === "center") {
        if (group[0]) positions.set(group[0].node_id, { x: center, y: height / 2 });
        continue;
      }
      const [sideText, distanceText] = key.split(":");
      const side = Number(sideText);
      const distance = Number(distanceText);
      const groupHeight = group.reduce((sum, node) => sum + cardHeights.get(node.node_id), 0)
        + Math.max(0, group.length - 1) * spacing.vertical;
      let y = (height - groupHeight) / 2;
      for (const node of group) {
        const cardHeight = cardHeights.get(node.node_id);
        positions.set(node.node_id, {
          x: center + side * cardStep * distance,
          y: y + cardHeight / 2,
        });
        y += cardHeight + spacing.vertical;
      }
    }
    const width = Math.max(viewportWidth, center + rightDepth * cardStep + GRAPH_CARD_WIDTH / 2 + margin);
    return { nodeMap, positions, width, height };
  }

  function renderGraph() {
    closeCardFilePreview();
    const nodesHost = $("graph-nodes");
    const svg = $("graph-links");
    const graphStage = $("graph-stage");
    const viewportWidth = Math.max(360, graphStage.clientWidth - 2);
    const nodes = [...state.graphData.nodes.values()];
    const edges = [...state.graphData.edges.values()];
    const edgeRoleMap = new Map();
    for (const edge of edges) {
      if (edge.relation === "maps_to") {
        const list = edgeRoleMap.get(edge.target_id) || [];
        list.push(...(edge.roles || []));
        edgeRoleMap.set(edge.target_id, [...new Set(list)]);
      }
    }
    if (!nodes.length && state.selectedNode) nodes.push({
      node_id: state.selectedNode.id, type: state.selectedNode.type, name: state.selectedNode.name,
      summary: state.selectedNode.summary || "", path: state.selectedNode.path || null,
      state: state.selectedNode.state || null,
    });
    const layout = graphLayout(nodes, edges, state.graphMode, viewportWidth, edgeRoleMap);
    nodesHost.replaceChildren();
    nodesHost.style.width = `${layout.width}px`;
    nodesHost.style.height = `${layout.height}px`;
    nodesHost.dataset.cardMode = state.graphCardMode;
    nodesHost.style.setProperty("--graph-card-width", `${GRAPH_CARD_WIDTH}px`);
    svg.style.width = `${layout.width}px`;
    svg.style.height = `${layout.height}px`;
    svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    svg.replaceChildren();
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.id = "arrowhead";
    marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7");
    marker.setAttribute("refX", "6"); marker.setAttribute("refY", "3.5"); marker.setAttribute("orient", "auto");
    const arrow = document.createElementNS("http://www.w3.org/2000/svg", "path");
    arrow.setAttribute("d", "M0,0 L7,3.5 L0,7 z"); arrow.setAttribute("fill", "#8eaaa0");
    marker.append(arrow); defs.append(marker); svg.append(defs);

    for (const edge of edges) {
      const source = layout.positions.get(edge.source_id);
      const target = layout.positions.get(edge.target_id);
      if (!source || !target) continue;
      const sx = source.x + (target.x >= source.x ? GRAPH_CARD_WIDTH / 2 : -GRAPH_CARD_WIDTH / 2);
      const tx = target.x + (target.x >= source.x ? -GRAPH_CARD_WIDTH / 2 : GRAPH_CARD_WIDTH / 2);
      const mid = (sx + tx) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M ${sx} ${source.y} C ${mid} ${source.y}, ${mid} ${target.y}, ${tx} ${target.y}`);
      path.classList.add(`${relationClass(edge.relation)}-edge`);
      if (edge.relation !== "related_to") path.setAttribute("marker-end", "url(#arrowhead)");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      const roleText = edge.roles?.length ? ` · ${edge.roles.join(", ")}` : "";
      title.textContent = `${edge.relation}${roleText}`;
      path.append(title); svg.append(path);
      if (state.graphMode === "relations") {
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", String((source.x + target.x) / 2));
        label.setAttribute("y", String((source.y + target.y) / 2 - 8));
        label.setAttribute("text-anchor", "middle");
        label.classList.add("edge-label");
        label.textContent = relationName(edge.relation);
        svg.append(label);
      }
    }

    if (!layout.positions.size) {
      renderGraphEmpty("当前范围没有可显示的关系");
      return;
    }
    for (const [nodeId, position] of layout.positions.entries()) {
      const node = layout.nodeMap.get(nodeId);
      if (!node) continue;
      const selected = nodeId === state.selectedNodeId || (node.type === "File" && node.path === state.sourcePath);
      const card = makeButton("", `graph-card${selected ? " selected" : ""}${node.type === "File" ? " file-node" : ""}`, async () => {
        closeCardFilePreview();
        if (node.type === "File") {
          const file = state.context?.files?.find((entry) => entry.path === node.path);
          await openSource(node.path, file || {});
          return;
        }
        await selectNode({ id: node.node_id, ...node });
      }, {
        "aria-label": node.type === "Concept"
          ? `${nodeTypeLabel(node.type)} ${node.name || node.path}。聚焦后显示关联文件预览，按向下键进入文件列表。`
          : `${nodeTypeLabel(node.type)} ${node.name || node.path}`,
        ...(node.type === "Concept" ? {
          "data-node-id": node.node_id,
          "aria-haspopup": "dialog",
          "aria-controls": "graph-file-preview",
          "aria-expanded": "false",
          "aria-description": "聚焦后显示此概念的 maps_to 文件映射；按向下键进入预览。",
        } : {}),
      });
      card.title = node.type === "Concept" ? "" : [node.name || node.path || node.node_id, node.summary].filter(Boolean).join("\n");
      card.style.left = `${position.x}px`;
      card.style.top = `${position.y}px`;
      if (node.type === "Concept") {
        card.addEventListener("pointerenter", (event) => {
          if (event.pointerType !== "touch") void showCardFilePreview(node, card);
        });
        card.addEventListener("pointerleave", scheduleCardPreviewClose);
        card.addEventListener("focus", () => {
          if (!state.suppressCardPreviewFocus) void showCardFilePreview(node, card);
        });
        card.addEventListener("blur", (event) => {
          const host = $("graph-file-preview");
          if (!host?.contains(event.relatedTarget)) scheduleCardPreviewClose();
        });
        card.addEventListener("keydown", async (event) => {
          if (event.key !== "ArrowDown") return;
          event.preventDefault();
          await showCardFilePreview(node, card);
          if (state.activeCardPreview?.node.node_id === node.node_id) {
            $("graph-file-preview")?.querySelector(".graph-file-preview-row, .graph-file-preview-action, .graph-file-preview-close")?.focus();
          }
        });
      }
      const head = el("span", "graph-card-head");
      head.append(el("span", "graph-card-type", nodeTypeLabel(node.type)));
      if (node.state) head.append(el("span", `graph-card-state${node.state === "tentative" ? " tentative" : ""}`));
      card.append(head, el("span", "graph-card-name", node.name || node.path || node.node_id));
      if (node.type === "File") {
        card.append(el("span", "graph-card-sub", node.path || ""));
        const roles = edgeRoleMap.get(node.node_id) || [];
        if (roles.length) {
          const roleList = el("span", "graph-role");
          for (const role of roles) roleList.append(el("span", "", role));
          card.append(roleList);
        }
      } else if (node.summary) {
        card.append(el("span", "graph-card-sub", node.summary));
      }
      nodesHost.append(card);
    }

    const result = state.graphResult;
    if (!result) {
      $("graph-completeness").textContent = "当前范围尚无结果";
      $("graph-more").hidden = true;
      return;
    }
    const displayedNodes = state.graphData.nodes.size;
    const displayedEdges = state.graphData.edges.size;
    const moduleFileReason = state.moduleFilesResult?.stop_reason;
    const reason = moduleFileReason && moduleFileReason !== "depth_limit"
      ? moduleFileReason : result.stop_reason;
    const hasMore = Boolean(state.graphCursor || state.moduleFilesCursor);
    const databaseEdgeCount = state.project?.semantic_map?.semantic_edge_count;
    const rangeLabel = state.graphMode === "relations"
      ? `${state.graphRelations.map(relationName).join(" + ")} · ${directionName(state.graphDirection)} · 1 hop`
      : state.graphMode === "files"
        ? "maps_to · 1 hop"
        : `contains · 双向 · ${state.graphDepth} hop`;
    let message;
    if (hasMore) {
      message = `当前查询仍有后续页 · 已载入 ${displayedNodes} 个节点 / ${displayedEdges} 条关系。`;
    } else if (reason && reason !== "depth_limit") {
      message = `当前视图不完整：${scopeReason(reason)} · ${displayedNodes} 节点 / ${displayedEdges} 关系。`;
    } else if (reason === "depth_limit") {
      message = `当前范围完整（${rangeLabel}）· ${displayedNodes} 节点 / ${displayedEdges} 边 · 数据库共 ${databaseEdgeCount ?? "—"} 条语义关系。`;
    } else {
      message = `当前范围完整（${rangeLabel}）· ${displayedNodes} 节点 / ${displayedEdges} 边 · 数据库共 ${databaseEdgeCount ?? "—"} 条语义关系。`;
    }
    $("graph-completeness").textContent = message;
    $("graph-status").classList.toggle("is-warning", Boolean(reason && reason !== "depth_limit"));
    $("graph-more").hidden = !hasMore;
    if (state.graphFocusPending) {
      const selectedPosition = layout.positions.get(state.selectedNodeId);
      if (selectedPosition) {
        graphStage.scrollLeft = Math.max(0, selectedPosition.x - viewportWidth / 2);
        graphStage.scrollTop = Math.max(0, selectedPosition.y - graphStage.clientHeight / 2);
      }
      state.graphFocusPending = false;
    }
  }

  async function loadContext(evidenceOffset = 0, append = false) {
    if (!state.projectId || !state.selectedNodeId || state.selectedNode?.type === "Project") return;
    const sequence = ++state.contextSequence;
    const projectId = state.projectId;
    const nodeId = state.selectedNodeId;
    const response = await postJson("/api/context", {
      project_id: projectId, node_id: nodeId,
      neighbor_limit: 20, evidence_offset: evidenceOffset, evidence_limit: 20,
    });
    if (sequence !== state.contextSequence || projectId !== state.projectId || nodeId !== state.selectedNodeId) return;
    if (append && state.context) {
      response.evidence = [...(state.context.evidence || []), ...(response.evidence || [])];
      response.evidence_page = response.evidence_page;
    }
    state.context = response;
    if (state.selectedNode) state.selectedNode = { ...state.selectedNode, ...response.node };
    renderInspector();
    $("footer-scope").textContent = response.truncated
      ? "当前检查器含有分页或预算截断内容"
      : "当前检查器查询范围完整";
  }

  function renderInspector() {
    const empty = $("inspector-empty");
    const projectPanel = $("project-inspector");
    const conceptPanel = $("concept-inspector");
    const sourcePanel = $("source-inspector");
    const node = state.selectedNode;
    empty.hidden = Boolean(node);
    projectPanel.hidden = !node || node.type !== "Project";
    conceptPanel.hidden = !node || !["Concept", "Module"].includes(node.type);
    sourcePanel.hidden = !state.sourcePath;
    if (!node) return;
    if (node.type === "Project") {
      renderProjectInspector();
    } else if (node.type === "Concept" || node.type === "Module") {
      renderConceptInspector();
    }
    if (state.sourcePath) renderSourceInspector();
  }

  function renderProjectInspector() {
    const project = state.project;
    if (!project) return;
    $("project-inspector-name").textContent = project.project_id;
    $("project-root").textContent = project.root || "项目根目录不可用";
    const last = project.last_successful_refresh;
    $("project-overview-meta").replaceChildren(
      statItem("文件数", project.discovered_file_count ?? "—"),
      statItem("目录数", project.discovered_directory_count ?? "—"),
      statItem("最近清单刷新", formatDate(last?.finished_at || last?.started_at)),
      statItem("刷新范围", last?.scope || "—"),
    );
    const coverage = project.semantic_map || {};
    $("project-coverage-meta").replaceChildren(
      statItem("Module / Concept", coverage.semantic_node_count ?? "—"),
      statItem("语义关系", coverage.semantic_edge_count ?? "—"),
      statItem("已映射文件", coverage.mapped_file_count ?? "—"),
      statItem("依据文件", coverage.evidence_file_count ?? "—"),
      statItem("最近观察", formatDate(coverage.freshness?.latest_observation_at)),
      statItem("未核验项", coverage.freshness?.unchecked_owner_count ?? "—"),
    );
    const relationCounts = $("relation-counts");
    relationCounts.replaceChildren();
    for (const relation of ["contains", "depends_on", "related_to", "maps_to"]) {
      const count = coverage.relation_counts?.[relation];
      if (count === undefined) continue;
      const item = el("span", "relation-count-chip");
      item.append(el("span", "", relationName(relation)), el("strong", "", count));
      relationCounts.append(item);
    }
    if (!relationCounts.childElementCount) {
      relationCounts.append(el("span", "tree-message", "后端尚未返回关系类型计数。"));
    }
  }

  function renderConceptInspector() {
    const context = state.context;
    const node = context?.node || state.selectedNode;
    if (!node) return;
    $("node-type").textContent = nodeTypeLabel(node.type);
    const stateLabel = $("node-state");
    stateLabel.textContent = node.state === "confirmed" ? "已确认" : node.state === "tentative" ? "待确认" : "—";
    stateLabel.className = `state-pill${node.state ? ` ${node.state}` : ""}`;
    $("node-name").textContent = node.name || node.id;
    $("node-summary").textContent = node.summary || "此概念尚无摘要。";
    const aliases = $("node-aliases");
    aliases.replaceChildren();
    for (const alias of node.aliases || []) aliases.append(el("span", "alias-chip", alias));
    const freshness = context?.node_freshness;
    $("node-times").replaceChildren(
      statItem("概念记录", formatDate(node.created_at)),
      statItem("摘要更新时间", formatDate(node.updated_at)),
      statItem("最近核验", formatDate(freshness?.observed_at)),
      statItem("最近观察", freshnessLabel(freshness).text),
    );

    const fileMeta = new Map((context?.files || []).map((file) => [file.path, file]));
    renderAssociations(fileMeta);
    renderEvidence(fileMeta);
    renderOtherRelations(fileMeta);
    const evidenceCount = context?.evidence_page?.total ?? context?.evidence?.length ?? 0;
    $("verify-button").disabled = evidenceCount === 0;
    const label = freshnessLabel(freshness);
    const explanation = freshnessExplanation(freshness);
    $("verify-note").textContent = evidenceCount === 0
      ? "此节点没有文件级依据可供核验。"
      : freshness?.observed_at
        ? `${label.text}${explanation ? ` · ${explanation}` : ""} · ${formatDate(freshness.observed_at)}。点击后只更新此节点的派生 freshness 观察。`
        : "尚无核验观察。点击后检查此节点的文件级依据并记录结果。";
  }

  function renderAssociations(fileMeta) {
    const list = $("association-list");
    list.replaceChildren();
    const edges = [...state.mappings.edges.values()].filter((edge) => edge.relation === "maps_to");
    $("association-count").textContent = state.mappingResult?.total_edges ?? edges.length;
    for (const edge of edges) {
      const fileNode = state.mappings.nodes.get(edge.target_id);
      if (!fileNode) continue;
      const path = fileNode.path || fileNode.name;
      const card = makeButton("", "file-card", () => openSource(path, fileMeta.get(path) || {}));
      const top = el("span", "file-card-top");
      top.append(el("span", "file-icon"), el("code", "file-path", path));
      card.append(top);
      const roleList = el("span", "file-role-list");
      for (const role of edge.roles?.length ? edge.roles : ["unspecified"]) roleList.append(el("span", "file-role", role));
      card.append(roleList);
      const metadata = fileMeta.get(path) || {};
      card.append(el("span", "file-card-meta", `清单 mtime ${formatMtime(metadata.mtime_ns)} · 记录 ${formatDate(metadata.indexed_at)}`));
      list.append(card);
    }
    if (!edges.length) list.append(el("div", "tree-message", "此 Concept 尚无 maps_to 文件关联。"));
    if (state.mappingCursor) {
      list.append(makeButton("继续载入关联文件 →", "text-button", async () => {
        await loadMappings(false);
      }));
    } else if (state.mappingResult?.stop_reason && state.mappingResult.stop_reason !== "depth_limit") {
      list.append(el("div", "tree-message", `文件关联结果不完整：${scopeReason(state.mappingResult.stop_reason)}`));
    }
  }

  function renderEvidence(fileMeta) {
    const list = $("evidence-list");
    list.replaceChildren();
    const evidence = state.context?.evidence || [];
    const freshness = state.context?.node_freshness;
    const ownerStatus = freshnessLabel(freshness);
    const stalePaths = new Set(freshness?.stale_paths || []);
    for (const item of evidence) {
      const meta = fileMeta.get(item.path) || {};
      const card = makeButton("", "evidence-card", () => openSource(item.path, meta));
      const top = el("span", "evidence-card-top");
      top.append(el("span", "file-icon"), el("code", "file-path", item.path));
      const pathStatus = stalePaths.has(item.path) ? "stale" : ownerStatus.className === "fresh" ? "fresh" : "unknown";
      top.append(el("span", `evidence-status ${pathStatus}`, ""));
      card.append(top);
      const metaLine = el("span", "evidence-meta");
      metaLine.append(el("span", "", `修改时间 ${formatMtime(meta.mtime_ns)}`));
      metaLine.append(el("span", "", `依据记录 ${formatDate(item.created_at)}`));
      card.append(metaLine);
      list.append(card);
    }
    if (!evidence.length) list.append(el("div", "tree-message", "此节点没有保存文件级依据。"));
    const page = state.context?.evidence_page;
    $("evidence-count").textContent = page?.total ?? evidence.length;
    const footer = $("evidence-footer");
    footer.replaceChildren();
    if (page?.next_offset !== null && page?.next_offset !== undefined) {
      footer.append(makeButton(`继续载入 · 已显示 ${evidence.length} / ${page.total}`, "text-button", async () => {
        await loadContext(page.next_offset, true);
      }));
    } else if (page && !page.complete) {
      footer.textContent = "依据列表未完整；当前查询受输出预算限制。";
    } else if (state.context?.truncated) {
      footer.textContent = "局部关系或祖先结果存在截断，其他区域可单独继续查询。";
    }
  }

  function renderOtherRelations(fileMeta) {
    const section = $("other-relations-section");
    const list = $("other-relations-list");
    list.replaceChildren();
    const relations = (state.context?.neighbors || []).filter((edge) => !["contains", "maps_to"].includes(edge.relation));
    section.hidden = !relations.length;
    $("other-relations-count").textContent = relations.length;
    for (const edge of relations) {
      const row = el("div", "relation-card");
      row.append(el("span", "relation-line"), el("code", "relation-label", edge.relation));
      const name = edge.peer?.name || edge.peer?.path || edge.peer?.id;
      row.append(makeButton(name, "relation-name", async () => {
        if (["Concept", "Module"].includes(edge.peer?.type)) await selectNode({ id: edge.peer.id, ...edge.peer });
      }));
      const edgeStatus = freshnessLabel(edge.freshness);
      const status = el("span", `relation-freshness ${edgeStatus.className}`, edgeStatus.text);
      status.title = freshnessExplanation(edge.freshness) || edgeStatus.text;
      row.append(status);
      if (edge.evidence?.length) {
        const verify = makeButton("核验", "relation-check", async () => {
          await verifyOwner("edge", edge.edge_id);
        });
        verify.title = `重新核验 ${edge.relation} 关系的文件级依据`;
        row.append(verify);
      }
      list.append(row);
      if (edge.evidence?.length) {
        for (const item of edge.evidence) {
          const meta = fileMeta.get(item.path) || {};
          const evidenceButton = makeButton(`${item.path} · ${formatDate(item.created_at)}`, "text-button", () => openSource(item.path, meta));
          evidenceButton.style.marginLeft = "30px";
          evidenceButton.style.textAlign = "left";
          list.append(evidenceButton);
        }
        if (edge.evidence_page?.next_offset !== null && edge.evidence_page?.next_offset !== undefined) {
          list.append(makeButton(`继续载入关系依据 · 已显示 ${edge.evidence.length} / ${edge.evidence_page.total}`, "text-button", async () => {
            await loadMoreEdgeEvidence(edge.edge_id, edge.evidence_page.next_offset);
          }));
        } else if (edge.evidence_page && !edge.evidence_page.complete) {
          list.append(el("div", "tree-message", "关系依据列表不完整，当前查询受输出预算限制。"));
        }
      }
    }
    const completeness = state.context?.completeness?.neighbors;
    section.hidden = !relations.length && (!completeness || completeness.complete);
    if (completeness && !completeness.complete) {
      const note = el("div", "tree-message", `直接关系区段显示 ${completeness.returned} / ${completeness.total} 条；${scopeReason(completeness.reason)}，未返回部分可能还包含其他关系。`);
      list.append(note);
    }
  }

  async function loadMoreEdgeEvidence(edgeId, offset) {
    const projectId = state.projectId;
    const nodeId = state.selectedNodeId;
    try {
      const response = await postJson("/api/context", {
        project_id: projectId, node_id: nodeId,
        neighbor_limit: 20, evidence_offset: offset, evidence_limit: 20,
      });
      if (projectId !== state.projectId || nodeId !== state.selectedNodeId) return;
      const page = response.neighbors?.find((edge) => edge.edge_id === edgeId);
      if (!page || !state.context) {
        showToast("关系依据已变化或不在当前关系页中，请重新打开此概念。", true);
        return;
      }
      state.context.neighbors = state.context.neighbors.map((edge) => edge.edge_id === edgeId
        ? { ...edge, evidence: [...(edge.evidence || []), ...(page.evidence || [])], evidence_page: page.evidence_page }
        : edge);
      renderInspector();
    } catch (error) {
      showToast(friendlyError(error), true);
    }
  }

  async function verifyOwner(ownerType, ownerId) {
    const projectId = state.projectId;
    const selectedNodeId = state.selectedNodeId;
    const verifyButton = ownerType === "node" ? $("verify-button") : null;
    if (verifyButton) {
      verifyButton.disabled = true;
      verifyButton.classList.add("busy");
      verifyButton.lastElementChild.textContent = "正在核验…";
    }
    try {
      const response = await postJson("/api/verify", {
        project_id: projectId, owner_type: ownerType, owner_id: ownerId,
      });
      const freshness = response.freshness;
      const label = freshnessLabel(freshness);
      const stats = response.freshness_check;
      const reason = freshnessExplanation(freshness);
      showToast(
        `${label.text}${reason ? ` · ${reason}` : ""} · `
        + `文件 ${freshness.checked_files}/${freshness.total_files}（上限 ${stats.file_limit}） · `
        + `读取 ${formatBytes(stats.bytes_hashed)}/${formatBytes(stats.byte_limit)} · `
        + `耗时 ${stats.elapsed_ms}/${stats.time_limit_ms} ms`
      );
      if (projectId === state.projectId && selectedNodeId === state.selectedNodeId) await loadContext();
    } catch (error) {
      showToast(friendlyError(error), true);
    } finally {
      if (verifyButton) {
        verifyButton.disabled = false;
        verifyButton.classList.remove("busy");
        verifyButton.lastElementChild.textContent = "重新核验依据";
      }
    }
  }

  async function openSource(path, metadata) {
    if (!path) return;
    const sequence = ++state.sourceSequence;
    state.previewSequence += 1;
    state.fileSearchSequence += 1;
    const projectId = state.projectId;
    state.sourcePath = path;
    state.sourceMeta = metadata || {};
    state.preview = null;
    state.previewNextLine = null;
    state.previewMode = "page";
    $("file-search-query").value = "";
    $("search-hits").hidden = true;
    $("search-hits").replaceChildren();
    renderInspector();
    if (state.sourceMeta.mtime_ns === undefined) {
      try {
        const response = await postJson("/api/list-files", {
          project_id: projectId, directory: path, include_directories: true, limit: 1, offset: 0,
        });
        if (sequence !== state.sourceSequence || projectId !== state.projectId || state.sourcePath !== path) return;
        state.sourceMeta = response.entries?.find((entry) => entry.path === path) || state.sourceMeta;
      } catch (_error) {
        // The file may exist outside the last successful manifest refresh.
      }
      renderSourceInspector();
    }
    await loadPreview(1, sequence);
  }

  function renderSourceInspector() {
    const path = state.sourcePath;
    if (!path) return;
    $("source-path").textContent = path;
    $("source-kind").textContent = path.split(".").pop()?.toUpperCase() || "FILE";
    const meta = state.sourceMeta || {};
    $("source-meta").replaceChildren(
      statItem(meta.source_mtime ? "文件当前 mtime" : "清单记录 mtime", formatMtime(meta.mtime_ns)),
      statItem("清单记录时间", formatDate(meta.indexed_at)),
      statItem("文件大小", formatBytes(meta.size)),
      statItem("依据状态", evidenceStatusForPath(path)),
    );
    $("source-inspector").hidden = false;
  }

  function evidenceStatusForPath(path) {
    const context = state.context;
    if (!context) return "未核验";
    const nodeHasEvidence = (context.evidence || []).some((item) => item.path === path);
    if (nodeHasEvidence) {
      const freshness = context.node_freshness;
      if (freshness?.stale_paths?.includes(path)) return "Concept 依据发现变化";
      if (freshness?.status === "stale") return "Concept 有其他依据过期";
      if (freshness?.status === "unknown") return "Concept 核验结果不完整";
      return `Concept ${freshnessLabel(freshness).text}`;
    }
    for (const relation of context.neighbors || []) {
      if (!(relation.evidence || []).some((item) => item.path === path)) continue;
      const freshness = relation.freshness;
      if (freshness?.stale_paths?.includes(path)) return `${relation.relation} 依据发现变化`;
      if (freshness?.status === "stale") return `${relation.relation} 有其他依据过期`;
      if (freshness?.status === "unknown") return `${relation.relation} 核验结果不完整`;
      return `${relation.relation} ${freshnessLabel(freshness).text}`;
    }
    return "此文件不是当前已显示的文件级依据";
  }

  async function loadPreview(startLine, sequence = state.sourceSequence) {
    if (!state.sourcePath) return;
    const requestSequence = ++state.previewSequence;
    const projectId = state.projectId;
    const path = state.sourcePath;
    const mode = state.previewMode;
    $("preview-status").className = "preview-status";
    $("preview-status").textContent = "读取本地文件…";
    try {
      const response = mode === "tail"
        ? await postJson("/api/preview-tail", { project_id: projectId, path, line_count: 80 })
        : await postJson("/api/preview", { project_id: projectId, path, start_line: startLine, line_count: 80 });
      if (sequence !== state.sourceSequence || requestSequence !== state.previewSequence
        || mode !== state.previewMode || projectId !== state.projectId || path !== state.sourcePath) return;
      state.preview = response;
      state.previewNextLine = mode === "page" ? response.next_start_line : null;
      renderPreview();
    } catch (error) {
      if (sequence !== state.sourceSequence || requestSequence !== state.previewSequence
        || mode !== state.previewMode || projectId !== state.projectId || path !== state.sourcePath) return;
      state.preview = null;
      $("preview-status").className = "preview-status warning";
      $("preview-status").textContent = friendlyError(error);
      $("source-lines").replaceChildren(el("code", "", friendlyError(error)));
      $("preview-range").textContent = "读取失败";
      $("preview-next").hidden = true;
    }
  }

  function renderPreview() {
    const preview = state.preview;
    if (!preview) return;
    const host = $("source-lines");
    host.replaceChildren();
    for (const line of preview.lines || []) {
      const row = el("span", "source-line");
      row.append(el("span", "line-number", line.line_number ?? "—"), el("span", "line-text", line.content));
      host.append(row);
    }
    if (!(preview.lines || []).length) host.append(el("code", "", "文件没有可显示的文本内容。"));
    host.scrollTop = 0;
    const status = $("preview-status");
    status.className = `preview-status${preview.truncated ? " warning" : ""}`;
    if (state.previewMode === "tail") {
      const lineNote = preview.line_numbers_complete ? "行号已确认" : "行号未确认（文件较大或达到索引预算）";
      status.textContent = preview.truncated
        ? `文件尾部未完整：${scopeReason(preview.reason)} · ${lineNote}${preview.continuation ? ` · ${preview.continuation}` : ""}`
        : `文件尾部 · ${lineNote}`;
    } else if (preview.reason === "file_end") {
      status.textContent = "已到文件末尾。";
    } else if (preview.truncated) {
      status.textContent = `预览未完整：${scopeReason(preview.reason)}${preview.continuation ? ` · ${preview.continuation}` : ""}`;
    } else {
      status.textContent = "从所选项目的当前文件读取。";
    }
    const lineNumbers = (preview.lines || []).map((line) => line.line_number).filter((line) => Number.isInteger(line));
    $("preview-range").textContent = lineNumbers.length
      ? `第 ${Math.min(...lineNumbers)}–${Math.max(...lineNumbers)} 行`
      : (state.previewMode === "tail" ? "文件尾部片段" : "行号未确认");
    $("preview-next").hidden = state.previewMode !== "page" || !state.previewNextLine;
  }

  async function searchCurrentFile(query) {
    if (!state.sourcePath || !query.trim()) return;
    const sequence = ++state.fileSearchSequence;
    const projectId = state.projectId;
    const path = state.sourcePath;
    const host = $("search-hits");
    host.hidden = false;
    host.replaceChildren(el("div", "tree-message", "搜索当前文件…"));
    try {
      const response = await postJson("/api/search-file", {
        project_id: projectId, path, query: query.trim(),
        limit: 50, context_lines: 2, case_sensitive: false,
      });
      if (sequence !== state.fileSearchSequence || projectId !== state.projectId || path !== state.sourcePath) return;
      host.replaceChildren();
      if (!response.results?.length) {
        host.append(el("div", "tree-message", response.outcome === "rg_unavailable" ? friendlyError(new ApiError(response)) : "当前文件没有匹配内容。"));
      }
      for (const hit of response.results || []) {
        const button = makeButton("", "search-hit", () => {
          state.previewMode = "page";
          loadPreview(hit.line_number || 1);
        });
        button.append(el("span", "search-hit-line", `:${hit.line_number}`), el("span", "search-hit-text", hit.snippet || hit.content || "匹配行"));
        host.append(button);
      }
      if (response.truncated) host.append(el("div", "tree-message", `搜索结果不完整：${scopeReason(response.reason)}`));
      if (!response.results?.length && response.reason) host.lastChild.textContent += ` · ${scopeReason(response.reason)}`;
    } catch (error) {
      if (sequence !== state.fileSearchSequence || projectId !== state.projectId || path !== state.sourcePath) return;
      host.replaceChildren(el("div", "tree-message", friendlyError(error)));
    }
  }

  async function openExternalFile() {
    if (!state.sourcePath) return;
    try {
      await postJson("/api/open-file", { project_id: state.projectId, path: state.sourcePath });
      showToast("已请求系统编辑器打开此文件。");
    } catch (error) {
      showToast(friendlyError(error), true);
    }
  }

  function loadGraphPreferences() {
    try {
      const saved = JSON.parse(localStorage.getItem(GRAPH_PREFERENCES_KEY) || "{}");
      if (Object.hasOwn(GRAPH_SPACING, saved.spacing)) state.graphSpacing = saved.spacing;
      if (["wrap", "fixed"].includes(saved.cardMode)) state.graphCardMode = saved.cardMode;
    } catch (_error) {
      // Keep session defaults when local browser storage is unavailable.
    }
    $("graph-spacing").value = state.graphSpacing;
    $("graph-card-mode").value = state.graphCardMode;
  }

  function saveGraphPreferences() {
    try {
      localStorage.setItem(GRAPH_PREFERENCES_KEY, JSON.stringify({
        spacing: state.graphSpacing,
        cardMode: state.graphCardMode,
      }));
    } catch (_error) {
      // The controls remain usable for this page even when storage is unavailable.
    }
  }

  function bindEvents() {
    ensureCardPreviewHost();
    $("changes-open").addEventListener("click", async () => {
      if (!$("changes-dialog").open) $("changes-dialog").showModal();
      await loadChangesPage(0);
    });
    $("changes-close").addEventListener("click", () => $("changes-dialog").close());
    $("changes-dialog").addEventListener("close", () => {
      state.changeSequence += 1;
      state.changeOwnerSequence += 1;
      state.changeLoading = false;
      $("changes-run").disabled = false;
      $("changes-next").disabled = false;
    });
    $("changes-form").addEventListener("submit", (event) => {
      event.preventDefault();
      loadChangesPage(0);
    });
    $("changes-include-unchanged").addEventListener("change", () => loadChangesPage(0));
    $("changes-next").addEventListener("click", () => {
      if (state.changeNextOffset !== null && state.changeNextOffset !== undefined) {
        loadChangesPage(state.changeNextOffset, { append: true });
      }
    });
    $("graph-spacing").addEventListener("change", (event) => {
      state.graphSpacing = event.target.value;
      state.graphFocusPending = true;
      saveGraphPreferences();
      renderGraph();
    });
    $("graph-card-mode").addEventListener("change", (event) => {
      state.graphCardMode = event.target.value;
      state.graphFocusPending = true;
      saveGraphPreferences();
      renderGraph();
    });
    $("view-hierarchy").addEventListener("click", () => {
      if (state.graphMode === "structure") return;
      state.graphMode = "structure";
      updateGraphTabs();
      loadGraph(true).catch((error) => showToast(friendlyError(error), true));
    });
    $("view-relations").addEventListener("click", () => {
      if (state.graphMode === "relations" || $("view-relations").disabled) return;
      state.graphMode = "relations";
      updateGraphTabs();
      loadGraph(true).catch((error) => showToast(friendlyError(error), true));
    });
    $("view-files").addEventListener("click", () => {
      if (state.graphMode === "files" || $("view-files").disabled) return;
      state.graphMode = "files";
      updateGraphTabs();
      loadGraph(true).catch((error) => showToast(friendlyError(error), true));
    });
    $("graph-depth-toggle").addEventListener("click", () => {
      state.graphDepth = state.graphDepth === 1 ? 2 : 1;
      updateGraphTabs();
      loadGraph(true).catch((error) => showToast(friendlyError(error), true));
    });
    for (const id of ["relation-depends", "relation-related", "relation-contains"]) {
      $(id).addEventListener("change", (event) => {
        updateGraphRelationFilters();
        if (!state.graphRelations.length) {
          event.target.checked = true;
          updateGraphRelationFilters();
          return;
        }
        loadGraph(true).catch((error) => showToast(friendlyError(error), true));
      });
    }
    $("graph-direction").addEventListener("change", () => {
      updateGraphRelationFilters();
      loadGraph(true).catch((error) => showToast(friendlyError(error), true));
    });
    $("graph-more").addEventListener("click", () => loadGraph(false).catch((error) => showToast(friendlyError(error), true)));
    $("tree-reload").addEventListener("click", async () => {
      state.treeCache.clear();
      state.expanded = new Set([state.rootId]);
      renderTree();
      await fetchTreeChildren(state.rootId, true);
    });
    $("tree-refresh").addEventListener("click", async () => {
      const button = $("tree-refresh");
      button.disabled = true;
      button.textContent = "扫描中…";
      try {
        const response = await postJson("/api/refresh", { project_id: state.projectId });
        const updated = state.projects.find((item) => item.project_id === state.projectId);
        if (updated) updated.discovered_file_count = response.discovered_file_count;
        setProjectMeta(updated || state.project);
        state.treeCache.clear();
        state.expanded = new Set([state.rootId]);
        renderTree();
        await fetchTreeChildren(state.rootId, true);
        if (state.selectedNodeId && state.selectedNodeId !== state.rootId) {
          await loadGraph(true).catch((error) => showToast(friendlyError(error), true));
        }
        showToast(`文件清单已更新：${response.discovered_file_count} 个文件。概念与关系仍需由 ChatGPT 生成。`);
      } catch (error) {
        showToast(friendlyError(error), true);
      } finally {
        button.disabled = false;
        button.textContent = "扫描文件";
      }
    });
    $("concept-search").addEventListener("input", (event) => {
      clearTimeout(state.searchTimer);
      const query = event.target.value;
      if (!query.trim()) {
        state.treeSearch = "";
        state.searchSequence += 1;
        renderTree();
        return;
      }
      state.searchTimer = setTimeout(() => runConceptSearch(query), 230);
    });
    $("verify-button").addEventListener("click", () => {
      if (state.selectedNodeId) verifyOwner("node", state.selectedNodeId);
    });
    $("source-head").addEventListener("click", () => {
      state.previewMode = "page";
      loadPreview(1);
    });
    $("source-tail").addEventListener("click", () => {
      state.previewMode = "tail";
      loadPreview(1);
    });
    $("source-open").addEventListener("click", openExternalFile);
    $("source-close").addEventListener("click", () => {
      state.sourceSequence += 1;
      state.previewSequence += 1;
      state.fileSearchSequence += 1;
      state.sourcePath = null;
      state.sourceMeta = null;
      state.preview = null;
      state.previewNextLine = null;
      renderInspector();
    });
    $("preview-next").addEventListener("click", () => {
      if (!state.previewNextLine) return;
      state.previewMode = "page";
      loadPreview(state.previewNextLine);
    });
    $("file-search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      searchCurrentFile($("file-search-query").value);
    });
    document.addEventListener("pointerdown", (event) => {
      const active = state.activeCardPreview;
      const host = $("graph-file-preview");
      if (active && !active.anchor.contains(event.target) && !host?.contains(event.target)) {
        closeCardFilePreview();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.activeCardPreview) {
        event.preventDefault();
        closeCardFilePreview({ restoreFocus: true });
        return;
      }
      if (event.key === "/" && !event.ctrlKey && !event.metaKey && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
        event.preventDefault();
        $("concept-search").focus();
      }
      if (event.key === "Escape" && state.sourcePath) {
        state.sourceSequence += 1;
        state.previewSequence += 1;
        state.fileSearchSequence += 1;
        state.sourcePath = null;
        state.sourceMeta = null;
        renderInspector();
      }
    });
    window.addEventListener("resize", () => {
      if (state.graphData.nodes.size) renderGraph();
    });
    window.addEventListener("scroll", positionCardFilePreview, true);
  }

  loadGraphPreferences();
  bindEvents();
  loadProjects();
})();
