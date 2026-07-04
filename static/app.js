const state = {
  mode: "short_drama",
  project: null,
  activeTab: "overview",
  config: null,
  loadingTimer: null,
  productionTimer: null,
  playbackRenderTimer: null,
  pendingProductionProject: null,
  assetOperations: {},
  assetReferenceSelections: {},
  assetGlobalConsistency: {},
  assetLibraryFilter: "all",
};
const MAX_VIDEO_REFERENCE_IMAGES = 9;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  composer: $("#composerSection"),
  loading: $("#loadingView"),
  workspace: $("#workspaceSection"),
  form: $("#creativeForm"),
  generateButton: $("#generateButton"),
  advancedToggle: $("#advancedToggle"),
  advancedFields: $("#advancedFields"),
  projectList: $("#projectList"),
  pageTitle: $("#pageTitle"),
  tabPanel: $("#tabPanel"),
  toast: $("#toast"),
  downloadJson: $("#downloadJsonButton"),
  downloadMarkdown: $("#downloadMarkdownButton"),
  produceVideo: $("#produceVideoButton"),
  deleteProject: $("#deleteProjectButton"),
  imagePreviewModal: $("#imagePreviewModal"),
  imagePreviewImage: $("#imagePreviewImage"),
  imagePreviewLabel: $("#imagePreviewLabel"),
  imagePreviewDownload: $("#imagePreviewDownload"),
  imagePreviewClose: $("#imagePreviewClose"),
};

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function cssAspectRatio(value) {
  return {
    "9:16": "9 / 16",
    "16:9": "16 / 9",
    "1:1": "1 / 1",
    "3:4": "3 / 4",
    "4:3": "4 / 3",
  }[String(value || "")] || "16 / 9";
}

function openImagePreview(url, label) {
  elements.imagePreviewImage.src = url;
  elements.imagePreviewImage.alt = label || "图片预览";
  elements.imagePreviewLabel.textContent = label || "图片预览";
  elements.imagePreviewDownload.href = url;
  elements.imagePreviewModal.classList.remove("hidden");
  document.body.classList.add("preview-open");
  elements.imagePreviewClose.focus();
}

function closeImagePreview() {
  elements.imagePreviewModal.classList.add("hidden");
  elements.imagePreviewImage.removeAttribute("src");
  elements.imagePreviewDownload.removeAttribute("href");
  document.body.classList.remove("preview-open");
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务返回了无法识别的内容。");
  }
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}

async function loadConfig() {
  state.config = await api("/api/config");
  const dot = $("#modelDot");
  const label = $("#modelLabel");
  if (state.config.textModelEnabled) {
    dot.classList.add("live");
    label.textContent = state.config.model;
  } else {
    label.textContent = "演示引擎 · 可离线体验";
  }
  const autoAssets = $("#autoAssetsInput");
  autoAssets.disabled = !state.config.imageModelEnabled;
  if (!state.config.imageModelEnabled) autoAssets.checked = false;
}

async function loadProjects() {
  const { projects } = await api("/api/projects");
  if (!projects.length) {
    elements.projectList.innerHTML =
      '<div class="empty-history">还没有项目<br />从一个念头开始吧</div>';
    return;
  }
  elements.projectList.innerHTML = projects
    .map(
      (project) => `
      <button class="history-item" data-project-id="${escapeHtml(project.id)}" type="button">
        <strong>${escapeHtml(project.title)}</strong>
        <small>${escapeHtml(project.modeLabel)} · ${new Date(project.createdAt).toLocaleDateString("zh-CN")}</small>
      </button>`,
    )
    .join("");
}

function switchView(view) {
  elements.composer.classList.toggle("hidden", view !== "composer");
  elements.loading.classList.toggle("hidden", view !== "loading");
  elements.workspace.classList.toggle("hidden", view !== "workspace");
  const hasProject = view === "workspace";
  elements.downloadJson.classList.toggle("hidden", !hasProject);
  elements.downloadMarkdown.classList.toggle("hidden", !hasProject);
  elements.produceVideo.classList.toggle("hidden", !hasProject);
  elements.deleteProject.classList.toggle("hidden", !hasProject);
}

function startLoadingMessages() {
  const messages = [
    "先寻找值得被看见的那个瞬间…",
    "编剧正在校准冲突与情绪曲线…",
    "选角导演在固定角色的视觉锚点…",
    "摄影指导正在排布镜头与光线…",
    "制片正在核对时长和交付清单…",
  ];
  let index = 0;
  $("#loadingStep").textContent = messages[index];
  state.loadingTimer = window.setInterval(() => {
    index = (index + 1) % messages.length;
    $("#loadingStep").textContent = messages[index];
  }, 1900);
}

function stopLoadingMessages() {
  window.clearInterval(state.loadingTimer);
}

function editableField(scope, id, field, label, value, rows = 3) {
  return `
    <form class="inline-editor" data-edit-form>
      <input type="hidden" name="scope" value="${escapeHtml(scope)}" />
      <input type="hidden" name="itemId" value="${escapeHtml(id || "")}" />
      <input type="hidden" name="field" value="${escapeHtml(field)}" />
      <label>${escapeHtml(label)}
        <textarea name="value" rows="${rows}">${escapeHtml(value || "")}</textarea>
      </label>
      <button class="ghost compact" type="submit">保存</button>
    </form>`;
}

function stagePromptEditor(project, stage, title) {
  return `
    <section class="stage-prompt-editor">
      <div>
        <span class="card-kicker">CUSTOM STAGE PROMPT</span>
        <h3>${escapeHtml(title)}</h3>
      </div>
      ${editableField(
        "stage",
        stage,
        stage,
        "本环节自定义提示词",
        project.stagePrompts?.[stage] || "",
        3,
      )}
    </section>`;
}

function assetOperationKey(action, ...parts) {
  return [
    state.project?.id || "project",
    action,
    ...parts.map(String),
  ].join(":");
}

function assetOperationMarkup(key) {
  const operation = state.assetOperations[key];
  const status = operation?.status || "idle";
  const message = operation?.message || "";
  return `<span class="asset-operation-status ${escapeHtml(status)}"
    data-asset-operation="${escapeHtml(key)}" aria-live="polite">${escapeHtml(message)}</span>`;
}

function setAssetOperation(key, status, message) {
  state.assetOperations[key] = { status, message };
  $$("[data-asset-operation]").forEach((element) => {
    if (element.dataset.assetOperation !== key) return;
    element.className = `asset-operation-status ${status}`;
    element.textContent = message;
  });
  $$("[data-asset-operation-button]").forEach((button) => {
    if (button.dataset.assetOperationButton !== key) return;
    button.disabled = status === "running";
    button.textContent =
      status === "running"
        ? button.dataset.busyLabel
        : button.dataset.idleLabel;
  });
}

function assetGallery(project, ownerType, ownerId) {
  const assets = array(project.assets).filter(
    (asset) =>
      asset.ownerType === ownerType && String(asset.ownerId) === String(ownerId),
  );
  if (!assets.length) {
    return '<div class="asset-empty">尚无参考图</div>';
  }
  return `
    <div class="asset-gallery">
      ${assets
        .map((asset) => {
          const operationKey = assetOperationKey("edit", asset.id);
          const operation = state.assetOperations[operationKey];
          const editing = operation?.status === "running";
          const sourceLabel = {
            uploaded: "用户上传",
            generated: "模型生成",
            "reference-generated": "参考图生成",
            edited: "模型修改",
          }[asset.source] || "参考图片";
          return `
          <figure>
            <button class="image-preview-button" data-image-preview="${escapeHtml(asset.url)}"
              data-image-label="${escapeHtml(sourceLabel)}" type="button">
              <img src="${escapeHtml(asset.url)}" alt="参考素材" loading="lazy" />
            </button>
            <figcaption>
              <span>${sourceLabel}</span>
              <span class="asset-caption-actions">
                <a class="ghost compact" href="${escapeHtml(asset.url)}" download>下载</a>
                <button class="ghost compact" data-edit-asset
                  data-asset-id="${escapeHtml(asset.id)}"
                  data-owner-type="${escapeHtml(ownerType)}"
                  data-asset-operation-button="${escapeHtml(operationKey)}"
                  data-idle-label="修改图片"
                  data-busy-label="修改中…"
                  type="button"
                  ${state.config?.imageEditModelEnabled && !editing ? "" : "disabled"}>
                  ${editing ? "修改中…" : state.config?.imageEditModelEnabled ? "修改图片" : "未配置编辑模型"}
                </button>
              </span>
            </figcaption>
            ${assetOperationMarkup(operationKey)}
          </figure>`;
        })
        .join("")}
    </div>`;
}

function assetReferenceKey(projectId, ownerType, ownerId) {
  return `${projectId}:${ownerType}:${ownerId}`;
}

function assetReferencePicker(project, ownerType, ownerId) {
  const assets = array(project.assets).filter((asset) => asset.type === "image");
  const key = assetReferenceKey(project.id, ownerType, ownerId);
  const selected = array(state.assetReferenceSelections[key]).filter((assetId) =>
    assets.some((asset) => String(asset.id) === String(assetId)),
  );
  const useGlobal = state.assetGlobalConsistency[key] !== false;
  const editEnabled = Boolean(state.config?.imageEditModelEnabled);
  const characterNames = new Map(
    array(project.characters).map((item) => [String(item.id), item.name]),
  );
  const sceneNames = new Map(
    array(project.scenes).map((item) => [String(item.id), item.name]),
  );
  return `
    <details class="asset-reference-picker">
      <summary>
        图生图参考 · 手动选择 ${selected.length} 张
        ${useGlobal ? '<span>全局一致性开启</span>' : ""}
      </summary>
      <label class="global-reference-toggle">
        <input type="checkbox" data-global-asset-reference
          data-owner-type="${escapeHtml(ownerType)}"
          data-owner-id="${escapeHtml(ownerId)}"
          ${useGlobal ? "checked" : ""}
          ${editEnabled ? "" : "disabled"} />
        自动参考项目已有角色与场景主图
      </label>
      <p class="reference-help">
        自动模式会在每次生成时读取项目当前主图；手动勾选的图片会优先作为图生图参考。
        手动勾选的角色会被要求全部出现在最终画面中。
        ${editEnabled ? "" : "需先配置图片编辑模型。"}
      </p>
      ${
        assets.length
          ? `<div class="generation-reference-options">
              ${assets
                .map((asset) => {
                  const ownerName =
                    asset.ownerType === "character"
                      ? characterNames.get(String(asset.ownerId)) || "角色"
                      : sceneNames.get(String(asset.ownerId)) || "场景";
                  return `
                    <label>
                      <input type="checkbox" data-generation-asset-reference
                        data-owner-type="${escapeHtml(ownerType)}"
                        data-owner-id="${escapeHtml(ownerId)}"
                        value="${escapeHtml(asset.id)}"
                        ${selected.includes(String(asset.id)) ? "checked" : ""}
                        ${editEnabled ? "" : "disabled"} />
                      <img src="${escapeHtml(asset.url)}" alt="${escapeHtml(ownerName)}参考图" loading="lazy" />
                      <span>${escapeHtml(ownerName)}</span>
                    </label>`;
                })
                .join("")}
            </div>`
          : '<div class="reference-empty">项目中还没有可选图片；先生成或上传第一张图。</div>'
      }
    </details>`;
}

function assetActions(project, ownerType, ownerId, prompt) {
  const operationKey = assetOperationKey(
    "generate",
    ownerType,
    ownerId,
  );
  const operation = state.assetOperations[operationKey];
  const generating = operation?.status === "running";
  return `
    <div class="asset-actions">
      ${assetReferencePicker(project, ownerType, ownerId)}
      <button class="ghost compact" data-generate-asset
        data-owner-type="${escapeHtml(ownerType)}"
        data-owner-id="${escapeHtml(ownerId)}"
        data-prompt="${encodeURIComponent(prompt || "")}"
        data-asset-operation-button="${escapeHtml(operationKey)}"
        data-idle-label="模型生成参考图"
        data-busy-label="生成中…"
        type="button" ${generating ? "disabled" : ""}>
        ${generating ? "生成中…" : "模型生成参考图"}</button>
      <label class="ghost compact upload-label">
        上传参考图
        <input data-upload-asset
          data-owner-type="${escapeHtml(ownerType)}"
          data-owner-id="${escapeHtml(ownerId)}"
          type="file" accept="image/png,image/jpeg,image/webp" />
      </label>
      ${assetOperationMarkup(operationKey)}
    </div>`;
}

function renderOverview(project) {
  const brief = project.brief || {};
  return `
    ${stagePromptEditor(project, "overview", "创意总览提示词")}
    <div class="overview-grid">
      <article class="info-card">
        <span class="card-kicker">01 / HOOK</span>
        ${editableField("brief", "", "hook", "前三秒钩子", brief.hook, 3)}
      </article>
      <article class="info-card">
        <span class="card-kicker">02 / CORE CONFLICT</span>
        ${editableField("brief", "", "coreConflict", "核心冲突", brief.coreConflict, 3)}
      </article>
      <article class="info-card">
        <span class="card-kicker">03 / AUDIENCE</span>
        ${editableField("brief", "", "audience", "目标受众", brief.audience, 2)}
      </article>
      <article class="info-card">
        <span class="card-kicker">04 / TONE</span>
        ${editableField("brief", "", "tone", "整体语气", brief.tone, 2)}
      </article>
      <article class="info-card wide">
        <span class="card-kicker">05 / CREATIVE GOAL</span>
        ${editableField("brief", "", "goal", "传播目标", brief.goal, 3)}
      </article>
    </div>`;
}

function renderScript(project) {
  const script = project.script || {};
  const beats = array(script.beats);
  return `
    ${stagePromptEditor(project, "script", "剧本扩写提示词")}
    <article class="script-card">
      <span class="card-kicker">LOGLINE</span>
      ${editableField("script", "", "logline", "一句话故事", script.logline, 3)}
      <span class="card-kicker">SYNOPSIS</span>
      ${editableField("script", "", "synopsis", "故事梗概", script.synopsis, 5)}
      <span class="card-kicker">NARRATION / DIALOGUE</span>
      ${editableField("script", "", "narration", "旁白 / 台词", script.narration, 7)}
      <div class="beats">
        ${beats
          .map(
            (beat) => `
            <div class="beat">
              <strong>${escapeHtml(beat.beat)}</strong>
              <span>${escapeHtml(beat.duration)} SEC</span>
              <p><b>${escapeHtml(beat.purpose)}</b><br />${escapeHtml(beat.content)}</p>
            </div>`,
          )
          .join("")}
      </div>
    </article>`;
}

function renderChat(project) {
  const history = array(project.chatHistory);
  const pending = project.pendingChatProposal;
  const enabled = Boolean(state.config?.textModelEnabled);
  return `
    <section class="project-chat">
      <div class="project-chat-head">
        <div>
          <span class="card-kicker">CONVERSATIONAL PROJECT EDITOR</span>
          <h3>和镜舟一起改项目</h3>
          <p>每轮都会把当前剧本、角色和分镜交给模型。模型只展示修改建议，只有你确认后才会写入项目；继续发送要求会重新生成建议。</p>
        </div>
        <span class="capability-badge ${enabled ? "ready" : ""}">
          <i></i><span>${enabled ? escapeHtml(state.config.model) : "文本模型未配置"}</span>
        </span>
      </div>
      <div class="project-chat-messages">
        ${
          history.length
            ? history
                .map(
                  (message) => `
                  <article class="chat-message ${message.role === "user" ? "user" : "assistant"}">
                    <b>${message.role === "user" ? "你" : "镜舟"}</b>
                    <p>${escapeHtml(message.content)}</p>
                    ${
                      message.role === "assistant" &&
                      Number.isFinite(Number(message.appliedOperations))
                        ? `<small>已应用 ${escapeHtml(message.appliedOperations)} 项项目修改</small>`
                        : message.role === "assistant" &&
                            Number.isFinite(Number(message.proposedOperations))
                          ? `<small>提出 ${escapeHtml(message.proposedOperations)} 项待确认修改</small>`
                        : ""
                    }
                  </article>`,
                )
                .join("")
            : `<div class="chat-empty">
                <strong>试着这样说</strong>
                <span>“把结尾改得更克制，并在第二个镜头后增加一个雨夜追逐镜头。”</span>
                <span>“新增一名负责提供线索的女性角色，并补全外观与声音设定。”</span>
              </div>`
        }
      </div>
      ${
        pending
          ? `<section class="chat-proposal">
              <div class="chat-proposal-head">
                <div>
                  <span class="card-kicker">PENDING CHANGES</span>
                  <h4>等待你确认的修改建议</h4>
                  <p>当前剧本尚未修改。你可以应用、放弃，或继续在下方补充要求让模型重新分析。</p>
                </div>
                <strong>${escapeHtml(pending.operationCount || array(pending.preview).length)} 项</strong>
              </div>
              <div class="chat-proposal-list">
                ${array(pending.preview)
                  .map(
                    (item) => `
                    <article class="chat-change">
                      <h5>${escapeHtml(item.title)}</h5>
                      <div>
                        <section>
                          <b>修改前</b>
                          <p>${escapeHtml(item.before || "—")}</p>
                        </section>
                        <section>
                          <b>建议修改为</b>
                          <p>${escapeHtml(item.after || "—")}</p>
                        </section>
                      </div>
                    </article>`,
                  )
                  .join("")}
              </div>
              <div class="chat-proposal-actions">
                <button class="primary" type="button"
                  data-chat-apply="${escapeHtml(pending.id)}">确认并应用修改</button>
                <button class="ghost danger" type="button"
                  data-chat-reject="${escapeHtml(pending.id)}">放弃本轮建议</button>
              </div>
            </section>`
          : ""
      }
      <form class="project-chat-form" data-project-chat-form>
        <textarea name="message" rows="4" maxlength="8000"
          placeholder="${pending ? "继续补充修改要求，模型会基于当前剧本重新生成建议…" : "告诉镜舟你想怎样修改剧本、角色或分镜…"}" required></textarea>
        <button class="primary" type="submit" ${enabled ? "" : "disabled"}>
          ${enabled ? (pending ? "继续调整建议" : "发送并生成修改建议") : "请先配置文本模型"}
        </button>
      </form>
    </section>`;
}

function renderCharacters(project) {
  const characters = array(project.characters);
  const scenes = array(project.scenes);
  const assets = array(project.assets).filter(
    (asset) => asset.type === "image",
  );
  return `
    ${stagePromptEditor(project, "characters", "角色和场景生成提示词")}
    <div class="asset-toolbar">
      <div><strong>角色和场景一致性资产</strong><span>初始化内容由模型生成；你可以手动增删改，也可以在“对话编辑”中让模型提出修改建议。</span></div>
      <button class="primary small" data-generate-all-assets type="button"
        ${state.config?.imageModelEnabled ? "" : "disabled"}>
        ${state.config?.imageModelEnabled ? "批量生成缺失参考图" : "请先配置图片模型"}
      </button>
    </div>
    <div class="management-create-grid">
      <form class="manual-create-form compact-create-form" data-add-character-form>
        <div><strong>手动增加角色</strong><span>创建后可继续补全设定和参考图。</span></div>
        <input name="name" placeholder="角色名称" required maxlength="100" />
        <input name="role" placeholder="角色功能 / 身份" maxlength="2000" />
        <textarea name="visualIdentity" rows="2" placeholder="外观、服装、发型与识别锚点"></textarea>
        <textarea name="personality" rows="1" placeholder="性格设定"></textarea>
        <input name="voice" placeholder="声音设定" maxlength="2000" />
        <button class="primary small" type="submit">增加角色</button>
      </form>
      <form class="manual-create-form compact-create-form" data-add-scene-form>
        <div><strong>手动增加场景</strong><span>定义空间锚点后可生成或上传参考图。</span></div>
        <input name="name" placeholder="场景名称" required maxlength="200" />
        <textarea name="imagePrompt" rows="3" placeholder="空间结构、时间天气、光线、陈设与材质"></textarea>
        <button class="primary small" type="submit">增加场景</button>
      </form>
    </div>
    <h3 class="section-title">角色 <span>${characters.length}</span></h3>
    <div class="character-grid compact-asset-grid">
      ${characters
        .map(
          (character) => `
          <article class="character-card compact-identity-card">
            ${assetGallery(project, "character", character.id)}
            <div class="character-head">
              <div class="character-avatar">${escapeHtml(character.name?.slice(0, 1) || "角")}</div>
              <div><h3>${escapeHtml(character.name)}</h3><p>${escapeHtml(character.role || "未设置角色功能")}</p></div>
              <button class="ghost compact danger card-delete" type="button"
                data-delete-character="${escapeHtml(character.id)}">删除</button>
            </div>
            <div class="character-appearances">
              <b>分镜出场</b><span>${array(character.appearances).length ? `${array(character.appearances).length} 个镜头` : "暂未绑定"}</span>
            </div>
            <details class="identity-details">
              <summary>编辑角色设定与图片</summary>
              ${editableField("character", character.id, "name", "名称", character.name, 1)}
              ${editableField("character", character.id, "role", "角色功能", character.role, 1)}
              ${editableField("character", character.id, "visualIdentity", "视觉锚点", character.visualIdentity, 2)}
              ${editableField("character", character.id, "personality", "性格", character.personality, 1)}
              ${editableField("character", character.id, "voice", "声音", character.voice, 1)}
              ${editableField("character", character.id, "imagePrompt", "角色图片提示词", character.imagePrompt, 3)}
              <div class="describe-character">
                <select data-description-asset>
                  <option value="">选择角色参考图</option>
                  ${assets
                    .filter(
                      (asset) =>
                        String(asset.ownerType) === "character" &&
                        String(asset.ownerId) === String(character.id),
                    )
                    .map(
                      (asset, index) =>
                        `<option value="${escapeHtml(asset.id)}">参考图 ${index + 1}</option>`,
                    )
                    .join("")}
                </select>
                <button class="ghost compact" type="button"
                  data-describe-character="${escapeHtml(character.id)}"
                  ${state.config?.visionModelEnabled ? "" : "disabled"}>
                  ${state.config?.visionModelEnabled ? "由参考图生成设定" : "请配置视觉模型"}
                </button>
              </div>
              ${assetActions(project, "character", character.id, character.imagePrompt)}
            </details>
          </article>`,
        )
        .join("")}
    </div>
    <h3 class="section-title">场景 <span>${scenes.length}</span></h3>
    <div class="scene-grid compact-asset-grid">
      ${scenes
        .map(
          (scene) => `
          <article class="character-card scene-card compact-identity-card">
            ${assetGallery(project, "scene", scene.id)}
            <div class="character-head scene-head">
              <div class="character-avatar">景</div>
              <div><h3>${escapeHtml(scene.name)}</h3><p>场景空间锚点</p></div>
              <button class="ghost compact danger card-delete" type="button"
                data-delete-scene="${escapeHtml(scene.id)}">删除</button>
            </div>
            <details class="identity-details">
              <summary>编辑场景设定与图片</summary>
              ${editableField("scene", scene.id, "name", "场景名称", scene.name, 1)}
              ${editableField("scene", scene.id, "imagePrompt", "场景图片提示词", scene.imagePrompt, 3)}
              ${assetActions(project, "scene", scene.id, scene.imagePrompt)}
            </details>
          </article>`,
        )
        .join("")}
    </div>`;
}

function promptBox(label, value) {
  return `
    <div class="prompt-box">
      <b>${escapeHtml(label)}：</b>${escapeHtml(value)}
      <button class="copy-button" data-copy="${encodeURIComponent(value || "")}" type="button">复制</button>
    </div>`;
}

function referenceEditor(project, shot) {
  const assets = array(project.assets);
  const characters = array(project.characters);
  const scenes = array(project.scenes);
  const characterNames = new Map(
    characters.map((character) => [String(character.id), character.name]),
  );
  const sceneNames = new Map(
    scenes.map((scene) => [String(scene.id), scene.name]),
  );
  const selected = new Set(array(shot.referenceAssetIds).map(String));
  const selectedCharacters = new Set(array(shot.characterIds).map(String));
  const assetLabel = (asset) => {
    if (asset.ownerType === "character") {
      return `角色 · ${characterNames.get(String(asset.ownerId)) || asset.prompt || asset.id}`;
    }
    return `场景 · ${sceneNames.get(String(asset.ownerId)) || asset.prompt || asset.id}`;
  };
  const assetOptions = (selectedId) =>
    assets
      .map(
        (asset) =>
          `<option value="${escapeHtml(asset.id)}"
            ${String(selectedId || "") === String(asset.id) ? "selected" : ""}>${escapeHtml(assetLabel(asset))}</option>`,
      )
      .join("");
  return `
    <form class="reference-editor" data-reference-form data-shot-id="${escapeHtml(shot.id)}">
      <strong>固定帧与一致性素材</strong>
      <div class="character-options">
        <span>出镜角色（可多选）</span>
        <div>
          ${characters
            .map(
              (character) => `
              <label>
                <input type="checkbox" name="characterIds" value="${escapeHtml(character.id)}"
                  ${selectedCharacters.has(String(character.id)) ? "checked" : ""} />
                <b>${escapeHtml(character.name)}</b>
                <small>${escapeHtml(character.role || "角色")}</small>
              </label>`,
            )
            .join("")}
        </div>
      </div>
      <span class="reference-help">
        所选图片会逐张绑定为 Seedance <code>reference_image</code>，
        每个分镜最多 ${MAX_VIDEO_REFERENCE_IMAGES} 张。
        <b data-video-reference-count>${selected.size}/${MAX_VIDEO_REFERENCE_IMAGES}</b>
      </span>
      <div class="reference-options">
        ${
          assets.length
            ? assets
                .map(
                  (asset) => `
                  <label>
                    <input type="checkbox" name="assetIds" data-video-reference
                      value="${escapeHtml(asset.id)}"
                      ${selected.has(String(asset.id)) ? "checked" : ""}
                      ${selected.size >= MAX_VIDEO_REFERENCE_IMAGES && !selected.has(String(asset.id)) ? "disabled" : ""} />
                    <img src="${escapeHtml(asset.url)}" alt="" loading="lazy" />
                    <span>${escapeHtml(assetLabel(asset))}</span>
                  </label>`,
                )
                .join("")
            : '<span class="reference-empty">尚无参考图，可先保存出镜角色，再到“角色”页生成或上传图片。</span>'
        }
      </div>
      <div class="frame-selectors">
        <label>起始参考帧
          <select name="startFrameAssetId">
            <option value="">不指定</option>${assetOptions(shot.startFrameAssetId)}
          </select>
        </label>
        <label>结束构图参考
          <select name="endFrameAssetId">
            <option value="">不指定</option>${assetOptions(shot.endFrameAssetId)}
          </select>
        </label>
        <button class="ghost compact" type="submit">保存素材绑定</button>
      </div>
    </form>`;
}

function storyboardVideoToolbar(project) {
  const production = project.videoProduction || {};
  const jobs = array(production.jobs);
  const shots = array(project.storyboard);
  const readyShots = shots.filter(
    (shot) =>
      String(shot.completeVideoPrompt || "").trim() &&
      !shot.completeVideoPromptStale,
  ).length;
  const allPromptsReady = shots.length > 0 && readyShots === shots.length;
  const enabled = Boolean(state.config?.videoModelEnabled);
  const active = ["queued", "running"].includes(production.status);
  const completed = jobs.filter((job) => job.status === "succeeded").length;
  const progress = jobs.length ? Math.round((completed / jobs.length) * 100) : 0;
  const failedCount = jobs.filter((job) => job.status !== "succeeded").length;
  const localClipCount = jobs.filter(
    (job) => job.status === "succeeded" && job.localVideoUrl,
  ).length;
  return `
    ${stagePromptEditor(project, "video", "视频生成总提示词")}
    <section class="storyboard-video-toolbar production-console">
      <div class="production-intro">
        <div>
          <span class="card-kicker">STORYBOARD TO VIDEO</span>
          <h3>在分镜中直接生成视频</h3>
          <p>先为每个分镜生成并确认完整视频提示词，再单独或整轮提交。当前已确认 ${readyShots}/${shots.length} 个。</p>
        </div>
        <span class="capability-badge ${enabled ? "ready" : ""}">
          <i></i><span>${enabled ? escapeHtml(state.config.videoModel) : "视频 API 未配置"}</span>
        </span>
      </div>
      <div class="production-settings">
        <label><span>分辨率</span>
          <select id="videoResolution">
            <option value="480p" ${production.settings?.resolution === "480p" ? "selected" : ""}>480p · 草稿</option>
            <option value="720p" ${!production.settings?.resolution || production.settings?.resolution === "720p" ? "selected" : ""}>720p · 推荐</option>
            <option value="1080p" ${production.settings?.resolution === "1080p" ? "selected" : ""}>1080p · 高清</option>
          </select>
        </label>
        <label class="toggle-setting">
          <input id="videoAudio" type="checkbox" ${production.settings?.generateAudio === false ? "" : "checked"} />
          <span>按提示词生成声音</span>
        </label>
        <label class="toggle-setting">
          <input id="videoContinuity" type="checkbox" ${production.settings?.continuity === false ? "" : "checked"} />
          <span>连续片段使用上一段尾帧</span>
        </label>
        <button class="primary small" data-start-video type="button"
          ${enabled && !active && allPromptsReady ? "" : "disabled"}>${active ? "视频生成中…" : allPromptsReady ? "按已确认提示词生成全部" : `还需确认 ${shots.length - readyShots} 个提示词`}</button>
      </div>
      ${
        jobs.length
          ? `<div class="production-progress" data-production-progress>
              <div><span data-production-progress-bar style="width:${progress}%"></span></div>
              <strong data-production-progress-label>${completed} / ${jobs.length}</strong>
            </div>`
          : ""
      }
      ${
        production.stale
          ? `<div class="stale-production-warning"><div><strong>分镜内容已经更新</strong><span>旧视频仍可观看；可单独生成修改后的镜头，或重新生成全部分镜。</span></div></div>`
          : ""
      }
      ${
        production.finalVideoUrl
          ? `<article class="final-video">
              <div><span class="card-kicker">FINAL CUT</span><h3>完整视频</h3><p>${escapeHtml(production.assembly?.message || "")}</p></div>
              <video controls preload="metadata" src="${escapeHtml(production.finalVideoUrl)}"></video>
              <a href="${escapeHtml(production.finalVideoUrl)}" download>下载完整 MP4</a>
            </article>`
          : ""
      }
      ${
        jobs.length
          ? `<div class="production-footer">
              ${!active && !production.stale && failedCount ? `<button class="ghost" data-retry-production type="button">重新生成失败镜头（${failedCount}）</button>` : ""}
              ${!active && !production.stale && localClipCount > 1 ? `<button class="primary small" data-assemble-production type="button" ${state.config?.ffmpegEnabled ? "" : "disabled"}>${state.config?.ffmpegEnabled ? `一键拼接全部镜头（${localClipCount}）` : "请配置 FFmpeg"}</button>` : ""}
              <button class="ghost" data-refresh-production type="button">刷新状态</button>
            </div>`
          : ""
      }
    </section>`;
}

function shotCompletePrompt(project, shot) {
  const production = project.videoProduction || {};
  const active = ["queued", "running"].includes(production.status);
  const completePrompt = String(shot.completeVideoPrompt || "").trim();
  const stale = Boolean(shot.completeVideoPromptStale);
  const ready = Boolean(completePrompt) && !stale;
  const referenceCount = array(shot.referenceAssetIds).length;
  const frameCount =
    Number(Boolean(shot.startFrameAssetId)) +
    Number(Boolean(shot.endFrameAssetId));
  return `
    <section class="shot-prompt-workflow ${ready ? "ready" : stale ? "stale" : ""}">
      <div class="shot-prompt-workflow-head">
        <div>
          <strong>完整视频提示词</strong>
          <span>已选 ${array(shot.characterIds).length} 个角色 · ${referenceCount} 张参考图 · ${frameCount} 个首尾帧</span>
        </div>
        <em>${ready ? "已确认可提交" : stale ? "素材已变化，请重新生成或保存确认" : "等待生成"}</em>
      </div>
      ${
        completePrompt
          ? `<form class="inline-editor complete-prompt-editor" data-edit-form>
              <input type="hidden" name="scope" value="shot" />
              <input type="hidden" name="itemId" value="${escapeHtml(shot.id)}" />
              <input type="hidden" name="field" value="completeVideoPrompt" />
              <label>可在提交视频前继续修改
                <textarea name="value" rows="5">${escapeHtml(completePrompt)}</textarea>
              </label>
              <button class="ghost compact" type="submit">保存并确认当前提示词</button>
            </form>`
          : `<p>Agent 会把本镜头草稿、完整故事上下文、角色/场景设定，以及所选图片各自的文本提示词交给文本模型扩写。</p>`
      }
      <div class="shot-prompt-actions">
        <button class="ghost compact" type="button"
          data-generate-video-prompt="${escapeHtml(shot.id)}"
          ${state.config?.textModelEnabled && !active ? "" : "disabled"}>
          ${completePrompt ? "重新生成完整提示词" : "生成完整视频提示词"}
        </button>
        <button class="primary small" type="button"
          data-generate-shot-video="${escapeHtml(shot.id)}"
          ${state.config?.videoModelEnabled && !active && ready ? "" : "disabled"}>
          ${ready ? "确认提示词与图片，生成视频" : "请先确认完整提示词"}
        </button>
      </div>
    </section>`;
}

function storyboardShotVideo(project, shot) {
  const production = project.videoProduction || {};
  const jobs = array(production.jobs).filter(
    (job) =>
      String(job.sourceShotId || "") === String(shot.id) ||
      (!job.sourceShotId && Number(job.sourceShot) === Number(shot.shot)),
  );
  const active = ["queued", "running"].includes(production.status);
  const thisShotActive =
    active &&
    (String(production.retryShotId || "") === String(shot.id) ||
      jobs.some((job) => ["pending", "queued", "running", "downloading"].includes(job.status)));
  return `
    <section class="shot-video-output">
      <div class="shot-video-heading">
        <div><strong>视频结果</strong><span>${jobs.length ? `${jobs.length} 个连续片段` : "尚未生成"}</span></div>
        <em>${thisShotActive ? "生成中…" : jobs.some((job) => job.localVideoUrl || job.videoUrl) ? "已有视频" : "等待提交"}</em>
      </div>
      ${jobs
        .map((job) => {
          const playbackUrl = job.localVideoUrl || job.videoUrl || "";
          const posterUrl = job.localLastFrameUrl || job.lastFrameUrl || "";
          const policyBlocked =
            Boolean(job.policyViolation) ||
            Boolean(shot.completeVideoPromptStale);
          return `
            <article class="shot-video-clip ${escapeHtml(job.status)}" data-production-job="${escapeHtml(job.id)}">
              <div class="job-heading">
                <div><strong>${escapeHtml(job.label)}</strong><span>${escapeHtml(job.duration)} 秒</span></div>
                <em data-production-job-status>${escapeHtml(productionStatusLabels[job.status] || job.status)}</em>
              </div>
              ${playbackUrl
                ? `<video controls playsinline preload="metadata" src="${escapeHtml(playbackUrl)}" ${posterUrl ? `poster="${escapeHtml(posterUrl)}"` : ""}></video>
                   <div class="library-card-actions">
                     <a class="ghost compact" href="${escapeHtml(playbackUrl)}" download>下载</a>
                     <button class="ghost compact" data-retry-job="${escapeHtml(job.id)}" type="button" ${active || production.stale || policyBlocked ? "disabled" : ""}>重生成该片段</button>
                   </div>`
                : `<p>${escapeHtml(job.error || (active ? "任务处理中，完成后一次性更新视频。" : "暂无视频文件。"))}</p>`}
              ${
                job.policyViolation
                  ? `<div class="policy-violation-note">
                      <strong>需要先原创化提示词</strong>
                      <span>该结果触发了版权/内容政策审核。请点击上方“重新生成完整提示词”，检查并保存后，再重新提交本分镜。</span>
                    </div>`
                  : ""
              }
              <details>
                <summary>查看本次参考图片、固定帧与完整提示词</summary>
                ${videoJobReferenceSummary(project, production, job)}
                <div class="prompt-box">${escapeHtml(job.prompt || "")}</div>
              </details>
            </article>`;
        })
        .join("")}
    </section>`;
}

function renderStoryboard(project) {
  return `
    ${stagePromptEditor(project, "storyboard", "分镜拆解与提示词")}
    <section class="storyboard-chat-entry">
      <div>
        <strong>用对话模型编辑分镜</strong>
        <span>提交时会携带完整剧本、全部角色和场景、所有分镜及发布设定，模型先展示修改建议，确认后才写入。</span>
      </div>
      <form data-project-chat-form>
        <textarea name="message" rows="2" maxlength="8000" placeholder="例如：让第 3 镜头承接上一镜头的动作，并缩短台词…" required></textarea>
        <button class="primary small" type="submit" ${state.config?.textModelEnabled ? "" : "disabled"}>生成修改建议</button>
      </form>
      ${
        project.pendingChatProposal
          ? `<div class="storyboard-pending-change">
              <span>模型已提出 ${escapeHtml(project.pendingChatProposal.operationCount || 0)} 项修改，等待确认。</span>
              <button class="primary small" data-chat-apply="${escapeHtml(project.pendingChatProposal.id)}" type="button">确认应用</button>
              <button class="ghost compact danger" data-chat-reject="${escapeHtml(project.pendingChatProposal.id)}" type="button">放弃</button>
            </div>`
          : ""
      }
    </section>
    ${storyboardVideoToolbar(project)}
    <form class="manual-create-form shot-create-form" data-add-shot-form>
      <div>
        <strong>手动增加分镜</strong>
        <span>新分镜默认追加到末尾，保存后可继续编辑完整提示词。</span>
      </div>
      <input name="scene" placeholder="场景" required />
      <input name="duration" type="number" min="1" max="180" value="5" required />
      <input name="camera" placeholder="机位与运镜" />
      <textarea name="action" rows="2" placeholder="镜头动作" required></textarea>
      <textarea name="dialogue" rows="2" placeholder="台词 / 旁白"></textarea>
      <div class="manual-shot-characters">
        <b>出镜角色</b>
        ${array(project.characters)
          .map(
            (character) => `
            <label>
              <input type="checkbox" name="characterIds"
                value="${escapeHtml(character.id)}" />
              <span>${escapeHtml(character.name)}</span>
            </label>`,
          )
          .join("")}
      </div>
      <button class="primary small" type="submit">增加分镜</button>
    </form>
    <div class="shot-list">
      ${array(project.storyboard)
        .map(
          (shot) => `
          <article class="shot-card">
            <div class="shot-index">
              <strong>${String(shot.shot).padStart(2, "0")}</strong>
              <span>${escapeHtml(shot.duration)} SEC</span>
            </div>
            <div class="shot-content">
              <div class="shot-title">
                <h3>${escapeHtml(shot.scene)}</h3>
                <div>
                  <span>${escapeHtml(shot.camera)}</span>
                  <button class="ghost compact danger" type="button"
                    data-delete-shot="${escapeHtml(shot.id)}">删除分镜</button>
                </div>
              </div>
              <p class="shot-summary">${escapeHtml(shot.action || "尚未填写镜头动作")}</p>
              <details class="shot-edit-details">
                <summary>编辑分镜草稿、出镜角色与参考图片</summary>
                <div class="shot-field-grid">
                  ${editableField("shot", shot.id, "scene", "场景", shot.scene, 1)}
                  ${editableField("shot", shot.id, "duration", "时长（秒）", shot.duration, 1)}
                  ${editableField("shot", shot.id, "action", "动作", shot.action, 2)}
                  ${editableField("shot", shot.id, "camera", "机位与运镜", shot.camera, 2)}
                  ${editableField("shot", shot.id, "dialogue", "台词 / 旁白", shot.dialogue, 2)}
                  ${editableField("shot", shot.id, "audio", "声音", shot.audio, 2)}
                  ${editableField("shot", shot.id, "visualPrompt", "关键帧图片提示词", shot.visualPrompt, 3)}
                  ${editableField("shot", shot.id, "videoPrompt", "分镜运动提示词（用于扩写）", shot.videoPrompt, 3)}
                  ${editableField("shot", shot.id, "continuity", "连续性", shot.continuity, 2)}
                </div>
                ${referenceEditor(project, shot)}
              </details>
              ${shotCompletePrompt(project, shot)}
              ${storyboardShotVideo(project, shot)}
            </div>
          </article>`,
        )
        .join("")}
    </div>`;
}

const productionStatusLabels = {
  pending: "等待提交",
  queued: "排队中",
  running: "生成中",
  downloading: "下载中",
  succeeded: "已完成",
  failed: "失败",
  expired: "已超时",
  cancelled: "已取消",
  partial: "部分完成",
};

function videoJobReferenceSummary(project, production, job) {
  const assets = array(project.assets);
  const assetById = new Map(
    assets.map((asset) => [String(asset.id), asset]),
  );
  const characterNames = new Map(
    array(project.characters).map((item) => [String(item.id), item.name]),
  );
  const sceneNames = new Map(
    array(project.scenes).map((item) => [String(item.id), item.name]),
  );
  const assetName = (asset) => {
    if (!asset) return "未知图片";
    if (asset.ownerType === "character") {
      return `角色 · ${characterNames.get(String(asset.ownerId)) || asset.id}`;
    }
    return `场景 · ${sceneNames.get(String(asset.ownerId)) || asset.id}`;
  };
  const references = array(job.referenceAssetIds)
    .map((assetId) => assetById.get(String(assetId)))
    .filter(Boolean);
  const startAsset =
    assetById.get(String(job.startFrameAssetId || "")) ||
    assets.find(
      (asset) =>
        job.startFrameUrl && String(asset.url) === String(job.startFrameUrl),
    );
  const endAsset =
    assetById.get(String(job.endFrameAssetId || "")) ||
    assets.find(
      (asset) =>
        job.endFrameUrl && String(asset.url) === String(job.endFrameUrl),
    );
  let inputSource = String(job.inputReferenceSource || "");
  let inputAsset = assetById.get(
    String(job.inputReferenceAssetId || ""),
  );
  const submittedReferenceIds = new Set(
    array(job.submittedReferenceAssetIds).map(String),
  );
  let inputDescription = "";
  if (!inputSource) {
    if (
      production.settings?.continuity &&
      Number(job.part || 1) > 1
    ) {
      inputSource = "planned_previous_tail";
    } else if (startAsset) {
      inputSource = "start_frame";
      inputAsset = startAsset;
    } else if (endAsset) {
      inputSource = "last_frame";
      inputAsset = null;
    } else if (references.length) {
      inputSource = "reference_images";
    } else {
      inputSource = "none";
    }
  }
  if (inputSource === "previous_part_tail") {
    inputDescription = "上一连续片段的实际尾帧";
  } else if (inputSource === "planned_previous_tail") {
    inputDescription = "计划优先使用上一连续片段尾帧；不可用时回退到首张参考图";
  } else if (inputSource === "start_frame") {
    inputDescription = `固定首帧 · ${assetName(inputAsset || startAsset)}`;
    inputAsset ||= startAsset;
  } else if (inputSource === "last_frame") {
    inputAsset = null;
    inputDescription = `固定尾帧 · ${assetName(endAsset)}`;
  } else if (
    inputSource === "reference_images" ||
    inputSource === "reference_image"
  ) {
    inputAsset = null;
    inputDescription = `未使用固定帧；${submittedReferenceIds.size || Math.min(references.length, MAX_VIDEO_REFERENCE_IMAGES)} 张角色/场景图作为 reference_image`;
  } else {
    inputDescription = "本任务没有固定首尾帧或可用参考图";
  }
  const submitted = Boolean(job.taskId);
  return `
    <section class="job-reference-audit">
      <div class="job-reference-title">
        <strong>本次提交素材</strong>
        <span>已选择的图片会以 <code>reference_image</code> 提交，最多 ${MAX_VIDEO_REFERENCE_IMAGES} 张；首尾固定帧使用独立角色</span>
      </div>
      <div class="job-reference-fixed">
        <b>${submitted ? "实际 API 固定输入帧" : "计划 API 固定输入帧"}</b>
        ${
          inputAsset
            ? `<div class="fixed-frame-item">
                <img src="${escapeHtml(inputAsset.url)}" alt="${escapeHtml(assetName(inputAsset))}" loading="lazy" />
                <span>${escapeHtml(inputDescription)}</span>
              </div>`
            : `<span>${escapeHtml(inputDescription)}</span>`
        }
      </div>
      <div>
        <b>角色/场景一致性参考图（${references.length}）</b>
        ${
          references.length
            ? `<div class="job-reference-grid">
                ${references
                  .map(
                    (asset) => `
                    <figure>
                      <img src="${escapeHtml(asset.url)}" alt="${escapeHtml(assetName(asset))}" loading="lazy" />
                      <figcaption>
                        <span>${escapeHtml(assetName(asset))}</span>
                        <em>${submittedReferenceIds.has(String(asset.id))
                          ? "已作为 reference_image 提交"
                          : !submitted && ["reference_images", "reference_image"].includes(inputSource)
                            ? "计划作为 reference_image 提交"
                            : inputAsset && String(inputAsset.id) === String(asset.id)
                              ? "已作为固定首帧"
                              : "仅通过提示词约束一致性"}</em>
                      </figcaption>
                    </figure>`,
                  )
                  .join("")}
              </div>`
            : '<span class="reference-empty">未绑定角色或场景参考图。</span>'
        }
      </div>
      ${
        endAsset
          ? `<div class="job-end-frame-note">
              <img src="${escapeHtml(endAsset.url)}" alt="${escapeHtml(assetName(endAsset))}" loading="lazy" />
              <span><b>固定尾帧 · ${escapeHtml(assetName(endAsset))}</b>
              ${submitted ? "已通过 last_frame 提交给 Seedance。" : "将通过 last_frame 提交给 Seedance。"}</span>
            </div>`
          : ""
      }
    </section>`;
}

function renderVideoProduction(project) {
  const production = project.videoProduction;
  const enabled = Boolean(state.config?.videoModelEnabled);
  if (!production) {
    return `
      ${stagePromptEditor(project, "video", "视频生成总提示词")}
      <section class="production-console">
        <div class="production-intro">
          <div>
            <span class="card-kicker">SEEDANCE VIDEO PIPELINE</span>
            <h3>让分镜真正开始运动</h3>
            <p>镜舟会通过中转站的 Seedance 接口依次提交镜头，携带角色、场景或固定帧，完成后保存到本机。每次生成都会产生中转站 API 费用。</p>
          </div>
          <div class="capability-badge ${enabled ? "ready" : ""}">
            <i></i>
            <span>${enabled ? escapeHtml(state.config.videoModel) : "API 尚未配置"}</span>
          </div>
        </div>
        <div class="production-settings">
          <label>
            <span>分辨率</span>
            <select id="videoResolution">
              <option value="480p">480p · 草稿</option>
              <option value="720p" selected>720p · 推荐</option>
              <option value="1080p">1080p · 高清</option>
            </select>
          </label>
          <label class="toggle-setting">
            <input id="videoAudio" type="checkbox" checked />
            <span>按提示词生成声音（取决于模型）</span>
          </label>
          <label class="toggle-setting">
            <input id="videoContinuity" type="checkbox" checked />
            <span>使用上一段尾帧衔接下一段（角色/场景参考图始终生效）</span>
          </label>
        </div>
        <div class="production-submit">
          <div>
            <strong>${array(project.storyboard).length} 个分镜</strong>
            <span>超出 15 秒的镜头会自动拆段并顺序生成</span>
          </div>
          <button class="generate-button" data-start-video type="button" ${enabled ? "" : "disabled"}>
            <span>${enabled ? "提交视频生产" : "请先配置 API"}</span><b>▶</b>
          </button>
        </div>
        ${
          enabled
            ? ""
            : `<div class="config-hint">在项目根目录创建 <code>.env</code>，填写 <code>VIDEO_API_BASE_URL</code>、<code>VIDEO_API_KEY</code> 与 <code>VIDEO_MODEL</code>，然后重启服务。</div>`
        }
      </section>`;
  }

  const jobs = array(production.jobs);
  const completed = jobs.filter((job) => job.status === "succeeded").length;
  const progress = jobs.length ? Math.round((completed / jobs.length) * 100) : 0;
  const active = ["queued", "running"].includes(production.status);
  const stale = Boolean(production.stale);
  const failedCount = jobs.filter((job) => job.status !== "succeeded").length;
  const localClipCount = jobs.filter(
    (job) => job.status === "succeeded" && job.localVideoUrl,
  ).length;
  const assembly = production.assembly || {};
  return `
    ${stagePromptEditor(project, "video", "视频生成总提示词")}
    <section class="production-console">
      <div class="production-intro">
        <div>
          <span class="card-kicker">SEEDANCE VIDEO PRODUCTION</span>
          <h3>${stale ? "分镜已更新，旧视频仍保留" : active ? "片场正在运转" : "本轮生产已结束"}</h3>
          <p>${escapeHtml(production.settings?.model)} · ${escapeHtml(production.settings?.resolution)} · ${escapeHtml(production.settings?.ratio)} · ${production.settings?.generateAudio ? "音画同生" : "静音视频"}</p>
        </div>
        <div class="capability-badge ${!stale && production.status === "succeeded" ? "ready" : ""}">
          <i></i><span data-production-status>${stale ? "待按新分镜生成" : escapeHtml(productionStatusLabels[production.status] || production.status)}</span>
        </div>
      </div>
      ${
        stale
          ? `<div class="stale-production-warning">
              <div>
                <strong>视频生产记录已与最新分镜同步标记为过期</strong>
                <span>${escapeHtml(production.staleReason || "角色或分镜已修改")}。下方旧视频仍可观看和下载，但不能再按旧任务重试或合片。</span>
                <small>最新分镜 ${array(project.storyboard).length} 个 · 旧任务 ${jobs.length} 个</small>
              </div>
              <button class="primary" data-start-video type="button"
                ${enabled ? "" : "disabled"}>
                按最新分镜重新生产
              </button>
            </div>`
          : ""
      }
      <div class="production-progress" data-production-progress>
        <div><span data-production-progress-bar style="width:${progress}%"></span></div>
        <strong data-production-progress-label>${completed} / ${jobs.length}</strong>
      </div>
      ${
        active
          ? `<div class="delivery-gate">
              <strong>${production.retryScope === "single" ? "正在重生成单个镜头" : "正在完成整轮生成"}</strong>
              <span>${
                production.retryScope === "single"
                  ? "正在重做的镜头仅显示进度，其他已完成视频仍可正常观看。"
                  : "当前只同步任务状态；全部镜头处理结束后，视频会一次性开放播放。"
              }</span>
            </div>`
          : ""
      }
      ${
        production.finalVideoUrl
          ? `<article class="final-video">
              <div><span class="card-kicker">FINAL CUT</span><h3>完整视频</h3><p>${escapeHtml(assembly.message)}</p></div>
              <video controls preload="metadata" src="${escapeHtml(production.finalVideoUrl)}"></video>
              <a href="${escapeHtml(production.finalVideoUrl)}" download>下载完整 MP4</a>
            </article>`
          : assembly.status && assembly.status !== "pending"
            ? `<div class="assembly-note ${assembly.status}"><strong>合片状态</strong><span>${escapeHtml(assembly.message)}</span></div>`
            : ""
      }
      <div class="production-jobs">
        ${jobs
          .map((job) => {
            const playbackUrl = job.localVideoUrl || job.videoUrl || "";
            const posterUrl =
              job.localLastFrameUrl || job.lastFrameUrl || "";
            const isLocalPlayback = Boolean(job.localVideoUrl);
            const sourceShot = array(project.storyboard).find(
              (shot) =>
                String(shot.id) === String(job.sourceShotId) ||
                Number(shot.shot) === Number(job.sourceShot),
            );
            return `
            <article class="production-job ${escapeHtml(job.status)}" data-production-job="${escapeHtml(job.id)}">
              <div class="job-heading">
                <div><strong>${escapeHtml(job.label)}</strong><span>${escapeHtml(job.duration)} 秒</span></div>
                <div class="job-actions">
                  <em data-production-job-status>${escapeHtml(productionStatusLabels[job.status] || job.status)}</em>
                  <button class="ghost compact" data-retry-job="${escapeHtml(job.id)}"
                    type="button" ${active || stale ? "disabled" : ""}>单独重新生成</button>
                </div>
              </div>
              ${videoJobReferenceSummary(project, production, job)}
              ${
                playbackUrl
                  ? `<div class="clip-player">
                       <video controls playsinline preload="metadata"
                         src="${escapeHtml(playbackUrl)}"
                         ${posterUrl ? `poster="${escapeHtml(posterUrl)}"` : ""}></video>
                       <span class="playback-source">${isLocalPlayback ? "本地文件" : "远程预览"}</span>
                     </div>
                     ${
                       isLocalPlayback
                         ? `<a href="${escapeHtml(job.localVideoUrl)}" download>下载此镜头</a>`
                         : `<a href="${escapeHtml(job.videoUrl)}" target="_blank" rel="noopener noreferrer">在新窗口打开远程视频</a>`
                     }
                     ${
                       job.error
                         ? `<p class="playback-warning">视频可以远程播放，但本地保存失败：${escapeHtml(job.error)}</p>`
                         : ""
                     }`
                  : `<p>${job.error ? escapeHtml(job.error) : active ? "整轮视频完成后将一次性开放播放，本阶段仅显示进度。" : "尚无视频结果。"}</p>`
              }
              <details>
                <summary>查看提交提示词</summary>
                <div class="prompt-box">${escapeHtml(job.prompt)}</div>
              </details>
              ${sourceShot ? referenceEditor(project, sourceShot) : ""}
            </article>`;
          })
          .join("")}
      </div>
      <div class="production-footer">
        <span>远程结果会在约 24 小时后失效；镜舟已将成功文件保存到本机。</span>
        ${
          !active && !stale && failedCount
            ? `<button class="ghost" data-retry-production type="button">重新生成失败镜头（${failedCount}）</button>`
            : ""
        }
        ${
          !active && !stale && localClipCount > 1
            ? `<button class="primary small" data-assemble-production type="button"
                ${state.config?.ffmpegEnabled ? "" : "disabled"}>
                ${state.config?.ffmpegEnabled
                  ? production.finalVideoUrl
                    ? `重新拼接全部镜头（${localClipCount}）`
                    : `一键拼接全部镜头（${localClipCount}）`
                  : "请先安装或配置 FFmpeg"}
              </button>`
            : ""
        }
        <button class="ghost" data-refresh-production type="button">刷新状态</button>
      </div>
    </section>`;
}

function renderPublish(project) {
  const value = project.deliverables || {};
  return `
    ${stagePromptEditor(project, "publish", "发布与交付提示词")}
    <div class="publish-grid">
      <article class="publish-card">
        <span class="card-kicker">TITLE OPTIONS</span>
        <h3>标题备选</h3>
        <ul>${array(value.titleOptions).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      </article>
      <article class="publish-card">
        <span class="card-kicker">SOCIAL COPY</span>
        <h3>发布文案</h3>
        ${editableField("deliverable", "", "caption", "发布文案", value.caption, 5)}
        <div class="tags">${array(value.hashtags).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
      </article>
      <article class="publish-card">
        <span class="card-kicker">COVER & MUSIC</span>
        <h3>封面与声音</h3>
        ${editableField("deliverable", "", "coverPrompt", "封面提示词", value.coverPrompt, 4)}
        ${editableField("deliverable", "", "musicPrompt", "配乐提示词", value.musicPrompt, 4)}
      </article>
      <article class="publish-card">
        <span class="card-kicker">QUALITY CONTROL</span>
        <h3>交付前检查</h3>
        <ul>${array(value.checklist).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      </article>
      <article class="publish-card wide">
        <span class="card-kicker">NEGATIVE PROMPT</span>
        <h3>通用负面提示词</h3>
        ${editableField("deliverable", "", "negativePrompt", "通用负面提示词", value.negativePrompt, 4)}
      </article>
    </div>`;
}

function renderAssetLibrary(project) {
  const assets = array(project.assets);
  const production = project.videoProduction || {};
  const jobs = array(production.jobs);
  const active = ["queued", "running"].includes(production.status);
  const videoAspect = cssAspectRatio(
    production.settings?.ratio || project.brief?.aspectRatio,
  );
  const characterNames = new Map(
    array(project.characters).map((item) => [String(item.id), item.name]),
  );
  const sceneNames = new Map(
    array(project.scenes).map((item) => [String(item.id), item.name]),
  );
  const filter = state.assetLibraryFilter || "all";
  const sourceLabel = {
    uploaded: "用户上传",
    generated: "模型生成",
    "reference-generated": "参考图生成",
    edited: "模型修改",
  };
  const ownerLabel = (asset) =>
    asset.ownerType === "character"
      ? `角色 · ${characterNames.get(String(asset.ownerId)) || "未命名角色"}`
      : `场景 · ${sceneNames.get(String(asset.ownerId)) || "未命名场景"}`;
  const imageMatches = (asset) => {
    if (filter === "all") return true;
    if (filter === "characters") return asset.ownerType === "character";
    if (filter === "scenes") return asset.ownerType === "scene";
    if (filter === "uploaded") return asset.source === "uploaded";
    if (filter === "generated") return asset.source !== "uploaded";
    return false;
  };
  const visibleImages = assets.filter(imageMatches);
  const showImages = filter !== "videos";
  const showVideos = filter === "all" || filter === "videos";
  const ownerOptions = [
    ...array(project.characters).map(
      (item) =>
        `<option value="character|${escapeHtml(item.id)}">角色 · ${escapeHtml(item.name)}</option>`,
    ),
    ...array(project.scenes).map(
      (item) =>
        `<option value="scene|${escapeHtml(item.id)}">场景 · ${escapeHtml(item.name)}</option>`,
    ),
  ].join("");
  const filters = [
    ["all", "全部", assets.length + jobs.length + (production.finalVideoUrl ? 1 : 0)],
    ["characters", "角色图片", assets.filter((item) => item.ownerType === "character").length],
    ["scenes", "场景图片", assets.filter((item) => item.ownerType === "scene").length],
    ["uploaded", "上传图片", assets.filter((item) => item.source === "uploaded").length],
    ["generated", "生成/修改图片", assets.filter((item) => item.source !== "uploaded").length],
    ["videos", "视频", jobs.filter((item) => item.localVideoUrl || item.videoUrl).length + (production.finalVideoUrl ? 1 : 0)],
  ];
  return `
    <section class="asset-library">
      <div class="asset-library-head">
        <div>
          <span class="card-kicker">PROJECT ASSET LIBRARY</span>
          <h3>项目资产库</h3>
          <p>统一管理生成图片、上传参考图、视频镜头与最终合片。删除图片时会同步清理分镜和固定帧绑定。</p>
        </div>
        <div class="library-upload">
          <select data-library-owner>${ownerOptions}</select>
          <label class="primary small upload-label">
            上传角色/场景图片
            <input data-library-upload type="file" accept="image/png,image/jpeg,image/webp" />
          </label>
        </div>
      </div>
      <div class="asset-library-filters">
        ${filters
          .map(
            ([value, label, count]) => `
              <button type="button" data-asset-library-filter="${value}"
                class="${filter === value ? "active" : ""}">
                ${label}<span>${count}</span>
              </button>`,
          )
          .join("")}
      </div>
      ${
        showImages
          ? `<section class="library-section">
              <h3>图片资产 <span>${visibleImages.length}</span></h3>
              ${
                visibleImages.length
                  ? `<div class="library-image-grid">
                      ${visibleImages
                        .map((asset) => {
                          const editKey = assetOperationKey("edit", asset.id);
                          return `
                          <article class="library-image-card">
                            <button class="image-preview-button" data-image-preview="${escapeHtml(asset.url)}"
                              data-image-label="${escapeHtml(ownerLabel(asset))}" type="button">
                              <img src="${escapeHtml(asset.url)}" alt="${escapeHtml(ownerLabel(asset))}" loading="lazy" />
                            </button>
                            <div>
                              <strong>${escapeHtml(ownerLabel(asset))}</strong>
                              <span>${escapeHtml(sourceLabel[asset.source] || "图片资产")}</span>
                              <p>${escapeHtml(asset.editPrompt || asset.prompt || "未填写提示词")}</p>
                            </div>
                            <div class="library-card-actions">
                              <button class="ghost compact" data-edit-asset
                                data-asset-id="${escapeHtml(asset.id)}"
                                data-owner-type="${escapeHtml(asset.ownerType)}"
                                data-asset-operation-button="${escapeHtml(editKey)}"
                                data-idle-label="修改图片" data-busy-label="修改中…"
                                type="button" ${state.config?.imageEditModelEnabled ? "" : "disabled"}>
                                修改图片
                              </button>
                              <a class="ghost compact" href="${escapeHtml(asset.url)}" download>下载</a>
                              <button class="ghost compact danger" data-delete-asset="${escapeHtml(asset.id)}"
                                type="button" ${active ? "disabled" : ""}>删除</button>
                            </div>
                            ${assetOperationMarkup(editKey)}
                          </article>`;
                        })
                        .join("")}
                    </div>`
                  : '<div class="asset-library-empty">该分类暂无图片资产。</div>'
              }
            </section>`
          : ""
      }
      ${
        showVideos
          ? `<section class="library-section">
              <h3>视频资产 <span>${jobs.length + (production.finalVideoUrl ? 1 : 0)}</span></h3>
              ${
                production.finalVideoUrl
                  ? `<article class="library-final-video">
                      <video controls preload="metadata" src="${escapeHtml(production.finalVideoUrl)}"
                        style="--video-aspect:${videoAspect}"></video>
                      <div><strong>最终合片</strong><span>${escapeHtml(production.assembly?.message || "完整视频")}</span></div>
                      <div class="library-card-actions">
                        <a class="ghost compact" href="${escapeHtml(production.finalVideoUrl)}" download>下载</a>
                        <button class="ghost compact danger" data-delete-video-asset="final"
                          type="button" ${active ? "disabled" : ""}>删除合片</button>
                      </div>
                    </article>`
                  : ""
              }
              <div class="library-video-grid">
                ${jobs
                  .map((job) => {
                    const playbackUrl = job.localVideoUrl || job.videoUrl || "";
                    return `
                    <article class="library-video-card">
                      ${
                        playbackUrl
                          ? `<video controls preload="metadata" src="${escapeHtml(playbackUrl)}"
                              style="--video-aspect:${videoAspect}"></video>`
                          : '<div class="library-video-placeholder">暂无视频文件</div>'
                      }
                      <div>
                        <strong>${escapeHtml(job.label || "视频镜头")}</strong>
                        <span>${escapeHtml(productionStatusLabels[job.status] || job.status)} · ${escapeHtml(job.duration || "")} 秒</span>
                      </div>
                      <div class="library-card-actions">
                        <button class="ghost compact" data-retry-job="${escapeHtml(job.id)}"
                          type="button" ${active || job.policyViolation ? "disabled" : ""}>${job.policyViolation ? "需先更新提示词" : "重新生成"}</button>
                        <button class="ghost compact danger" data-delete-video-asset="${escapeHtml(job.id)}"
                          type="button" ${active || !playbackUrl ? "disabled" : ""}>删除视频</button>
                      </div>
                    </article>`;
                  })
                  .join("")}
              </div>
              ${!jobs.length && !production.finalVideoUrl ? '<div class="asset-library-empty">尚无视频资产。</div>' : ""}
            </section>`
          : ""
      }
    </section>`;
}

const renderers = {
  overview: renderOverview,
  script: renderScript,
  chat: renderChat,
  characters: renderCharacters,
  assets: renderAssetLibrary,
  storyboard: renderStoryboard,
  publish: renderPublish,
};

function renderActiveTab() {
  elements.tabPanel.innerHTML = renderers[state.activeTab](state.project);
  $$("#resultTabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.activeTab);
  });
}

function clearDeferredProductionRender() {
  window.clearTimeout(state.playbackRenderTimer);
  state.playbackRenderTimer = null;
  state.pendingProductionProject = null;
}

function hasActiveVideoPlayback() {
  return [...elements.tabPanel.querySelectorAll("video")].some(
    (video) => !video.paused && !video.ended,
  );
}

function updateProductionProgressInPlace(project) {
  const production = project?.videoProduction;
  if (!production || state.activeTab !== "storyboard") return;
  const jobs = array(production.jobs);
  const completed = jobs.filter((job) => job.status === "succeeded").length;
  const progress = jobs.length ? Math.round((completed / jobs.length) * 100) : 0;
  const bar = elements.tabPanel.querySelector("[data-production-progress-bar]");
  const label = elements.tabPanel.querySelector("[data-production-progress-label]");
  const status = elements.tabPanel.querySelector("[data-production-status]");
  if (bar) bar.style.width = `${progress}%`;
  if (label) label.textContent = `${completed} / ${jobs.length}`;
  if (status) {
    status.textContent =
      productionStatusLabels[production.status] || production.status;
  }
  jobs.forEach((job) => {
    const card = elements.tabPanel.querySelector(
      `[data-production-job="${String(job.id)}"]`,
    );
    const jobStatus = card?.querySelector("[data-production-job-status]");
    if (jobStatus) {
      jobStatus.textContent = productionStatusLabels[job.status] || job.status;
    }
  });
}

function scheduleDeferredProductionRender() {
  window.clearTimeout(state.playbackRenderTimer);
  state.playbackRenderTimer = window.setTimeout(() => {
    const project = state.pendingProductionProject;
    if (!project) return;
    if (hasActiveVideoPlayback()) {
      scheduleDeferredProductionRender();
      return;
    }
    state.pendingProductionProject = null;
    renderProject(project, { preserveTab: true });
  }, 750);
}

function applyProductionUpdate(project) {
  if (state.activeTab === "storyboard" && hasActiveVideoPlayback()) {
    state.project = project;
    state.pendingProductionProject = project;
    updateProductionProgressInPlace(project);
    scheduleDeferredProductionRender();
    scheduleProductionPolling();
    return;
  }
  renderProject(project, { preserveTab: true });
}

function renderProject(project, options = {}) {
  clearDeferredProductionRender();
  state.project = project;
  if (!options.preserveTab) state.activeTab = "overview";
  $("#projectMode").textContent = `${project.modeLabel} · ${project.engine === "demo" ? "DEMO" : "MODEL"}`;
  $("#projectTitle").textContent = project.title;
  $("#projectLogline").textContent = project.script?.logline || "";
  $("#projectDuration").textContent = project.brief?.durationSeconds || "—";
  elements.pageTitle.textContent = "一部作品，正在靠岸。";
  renderActiveTab();
  switchView("workspace");
  scheduleProductionPolling();
}

function scheduleProductionPolling() {
  window.clearTimeout(state.productionTimer);
  const status = state.project?.videoProduction?.status;
  if (!["queued", "running"].includes(status)) return;
  state.productionTimer = window.setTimeout(async () => {
    try {
      const project = await api(`/api/projects/${state.project.id}`);
      if (
        ["queued", "running"].includes(
          project?.videoProduction?.status,
        )
      ) {
        scheduleProductionPolling();
        return;
      }
      applyProductionUpdate(project);
    } catch (error) {
      showToast(`状态刷新失败：${error.message}`);
      scheduleProductionPolling();
    }
  }, 5000);
}

async function startVideoProduction() {
  if (!state.config?.videoModelEnabled) {
    showToast("请先配置中转站视频 API。");
    return;
  }
  const previousSettings =
    state.project.videoProduction?.settings || {};
  const resolution =
    $("#videoResolution")?.value ||
    previousSettings.resolution ||
    "720p";
  const audioInput = $("#videoAudio");
  const continuityInput = $("#videoContinuity");
  const watermarkInput = $("#videoWatermark");
  const generateAudio = audioInput
    ? audioInput.checked
    : previousSettings.generateAudio !== false;
  const continuity = continuityInput
    ? continuityInput.checked
    : previousSettings.continuity !== false;
  const watermark = watermarkInput
    ? watermarkInput.checked
    : Boolean(previousSettings.watermark);
  const confirmed = window.confirm(
    `将向中转站提交整个项目的视频任务，并产生 API 费用。\n\n分辨率：${resolution}\n镜头数：${array(state.project.storyboard).length}\n\n确认继续吗？`,
  );
  if (!confirmed) return;
  try {
    const production = await api(
      `/api/projects/${state.project.id}/video-production`,
      {
        method: "POST",
        body: JSON.stringify({
          resolution,
          ratio: state.project.brief?.aspectRatio || "9:16",
          generateAudio,
          continuity,
          watermark,
          confirmedPromptWorkflow: true,
        }),
      },
    );
    state.project.videoProduction = production;
    state.activeTab = "storyboard";
    renderActiveTab();
    scheduleProductionPolling();
    showToast("视频任务已进入生产队列。");
  } catch (error) {
    showToast(error.message);
  }
}

async function generateShotVideoPrompt(shotId, button) {
  if (!state.config?.textModelEnabled) {
    showToast("请先配置文本模型。");
    return;
  }
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在整合故事与参考图提示…";
  try {
    const project = await api(
      `/api/projects/${state.project.id}/shots/${shotId}/video-prompt`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
    renderProject(project, { preserveTab: true });
    showToast("完整视频提示词已生成，请检查、修改并确认。");
  } catch (error) {
    button.disabled = false;
    button.textContent = originalLabel;
    showToast(error.message);
  }
}

async function startShotVideo(shotId) {
  if (!state.config?.videoModelEnabled) {
    showToast("请先配置中转站视频 API。");
    return;
  }
  const shot = array(state.project.storyboard).find(
    (item) => String(item.id) === String(shotId),
  );
  if (!shot) {
    showToast("分镜不存在。");
    return;
  }
  if (
    !String(shot.completeVideoPrompt || "").trim() ||
    shot.completeVideoPromptStale
  ) {
    showToast("请先生成并确认当前分镜的完整视频提示词。");
    return;
  }
  const previousSettings = state.project.videoProduction?.settings || {};
  const resolution =
    $("#videoResolution")?.value || previousSettings.resolution || "720p";
  const generateAudio =
    $("#videoAudio")?.checked ?? previousSettings.generateAudio !== false;
  const continuity =
    $("#videoContinuity")?.checked ?? previousSettings.continuity !== false;
  if (
    !window.confirm(
      `将使用当前已确认的完整提示词和参考图片生成分镜 ${shot.shot}，并产生视频 API 费用。\n\n确认继续吗？`,
    )
  ) {
    return;
  }
  try {
    const production = await api(
      `/api/projects/${state.project.id}/shots/${shotId}/video`,
      {
        method: "POST",
        body: JSON.stringify({
          resolution,
          ratio: state.project.brief?.aspectRatio || "9:16",
          generateAudio,
          continuity,
        }),
      },
    );
    applyProductionUpdate({
      ...state.project,
      videoProduction: production,
    });
    scheduleProductionPolling();
    showToast(`分镜 ${shot.shot} 已进入视频生成队列。`);
  } catch (error) {
    showToast(error.message);
  }
}

async function retryFailedProduction() {
  const jobs = array(state.project?.videoProduction?.jobs);
  const failedCount = jobs.filter((job) => job.status !== "succeeded").length;
  if (!failedCount) {
    showToast("没有需要重新生成的失败镜头。");
    return;
  }
  const confirmed = window.confirm(
    `将重新提交 ${failedCount} 个失败镜头，并再次产生 API 费用。\n\n已成功的镜头不会重复生成。确认继续吗？`,
  );
  if (!confirmed) return;
  try {
    const production = await api(
      `/api/projects/${state.project.id}/video-production/retry`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
    applyProductionUpdate({
      ...state.project,
      videoProduction: production,
    });
    showToast("失败镜头已重新进入生产队列。");
  } catch (error) {
    showToast(error.message);
  }
}

async function assembleProduction(button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在拼接…";
  try {
    const production = await api(
      `/api/projects/${state.project.id}/video-production/assemble`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
    applyProductionUpdate({
      ...state.project,
      videoProduction: production,
    });
    if (production.finalVideoUrl) {
      showToast("视频拼接完成，可以直接播放或下载。");
    } else {
      showToast(production.assembly?.message || "视频拼接未完成。");
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function refreshProduction() {
  try {
    const project = await api(`/api/projects/${state.project.id}`);
    applyProductionUpdate(project);
    showToast("生产状态已刷新。");
  } catch (error) {
    showToast(error.message);
  }
}

async function saveEditableField(form) {
  const formData = new FormData(form);
  const project = await api(`/api/projects/${state.project.id}/field`, {
    method: "PATCH",
    body: JSON.stringify({
      scope: formData.get("scope"),
      id: formData.get("itemId"),
      field: formData.get("field"),
      value: formData.get("value"),
    }),
  });
  renderProject(project, { preserveTab: true });
  showToast("修改已保存。");
}

async function saveReferenceBinding(form) {
  const assetIds = [...form.querySelectorAll('input[name="assetIds"]:checked')].map(
    (input) => input.value,
  );
  const characterIds = [
    ...form.querySelectorAll('input[name="characterIds"]:checked'),
  ].map((input) => input.value);
  if (assetIds.length > MAX_VIDEO_REFERENCE_IMAGES) {
    showToast(`每个分镜最多选择 ${MAX_VIDEO_REFERENCE_IMAGES} 张参考图。`);
    return;
  }
  const shotId = form.dataset.shotId;
  const updates = [
    ["characterIds", characterIds],
    ["referenceAssetIds", assetIds],
    ["startFrameAssetId", form.elements.startFrameAssetId.value],
    ["endFrameAssetId", form.elements.endFrameAssetId.value],
  ];
  let project = state.project;
  for (const [field, value] of updates) {
    project = await api(`/api/projects/${state.project.id}/field`, {
      method: "PATCH",
      body: JSON.stringify({
        scope: "shot",
        id: shotId,
        field,
        value,
      }),
    });
  }
  renderProject(project, { preserveTab: true });
  showToast("分镜参考素材已更新。");
}

async function sendProjectChat(form) {
  const button = form.querySelector('button[type="submit"]');
  const message = form.elements.message.value.trim();
  if (!message) return;
  button.disabled = true;
  button.textContent = "正在分析当前剧本…";
  try {
    const result = await api(
      `/api/projects/${state.project.id}/chat`,
      {
        method: "POST",
        body: JSON.stringify({ message }),
      },
    );
    renderProject(result.project, { preserveTab: true });
    showToast(
      result.proposedOperations
        ? `模型提出 ${result.proposedOperations} 项修改，请确认。`
        : "模型已回复，本轮没有提出项目修改。",
    );
  } finally {
    button.disabled = false;
  }
}

async function resolveChatProposal(proposalId, accept) {
  const action = accept ? "apply" : "reject";
  const result = await api(
    `/api/projects/${state.project.id}/chat/${action}`,
    {
      method: "POST",
      body: JSON.stringify({ proposalId }),
    },
  );
  renderProject(result.project, { preserveTab: true });
  showToast(
    accept
      ? `已应用 ${result.appliedOperations} 项修改。`
      : "已放弃本轮修改建议。",
  );
}

async function addManualCharacter(form) {
  const formData = new FormData(form);
  const project = await api(
    `/api/projects/${state.project.id}/characters`,
    {
      method: "POST",
      body: JSON.stringify({
        name: formData.get("name"),
        role: formData.get("role"),
        visualIdentity: formData.get("visualIdentity"),
        personality: formData.get("personality"),
        voice: formData.get("voice"),
      }),
    },
  );
  renderProject(project, { preserveTab: true });
  showToast("新角色已增加。");
}

async function addManualScene(form) {
  const formData = new FormData(form);
  const project = await api(
    `/api/projects/${state.project.id}/scenes`,
    {
      method: "POST",
      body: JSON.stringify({
        name: formData.get("name"),
        imagePrompt: formData.get("imagePrompt"),
      }),
    },
  );
  renderProject(project, { preserveTab: true });
  showToast("新场景已增加。");
}

async function addManualShot(form) {
  const formData = new FormData(form);
  const characterIds = [
    ...form.querySelectorAll('input[name="characterIds"]:checked'),
  ].map((input) => input.value);
  const project = await api(
    `/api/projects/${state.project.id}/shots`,
    {
      method: "POST",
      body: JSON.stringify({
        scene: formData.get("scene"),
        duration: Number(formData.get("duration") || 5),
        camera: formData.get("camera"),
        action: formData.get("action"),
        dialogue: formData.get("dialogue"),
        characterIds,
      }),
    },
  );
  renderProject(project, { preserveTab: true });
  showToast("新分镜已增加。");
}

async function deleteShot(shotId) {
  if (!window.confirm("确定删除这个分镜吗？分镜编号会自动重排。")) {
    return;
  }
  const project = await api(
    `/api/projects/${state.project.id}/shots/${shotId}`,
    { method: "DELETE" },
  );
  renderProject(project, { preserveTab: true });
  showToast("分镜已删除。");
}

async function deleteIdentity(type, itemId) {
  const label = type === "character" ? "角色" : "场景";
  if (!window.confirm(`确定删除这个${label}及其项目内参考图吗？`)) {
    return;
  }
  const project = await api(
    `/api/projects/${state.project.id}/${type === "character" ? "characters" : "scenes"}/${itemId}`,
    { method: "DELETE" },
  );
  renderProject(project, { preserveTab: true });
  showToast(`${label}已删除。`);
}

async function describeCharacter(button) {
  const select = button
    .closest(".describe-character")
    ?.querySelector("[data-description-asset]");
  const assetId = select?.value || "";
  if (!assetId) {
    showToast("请先选择一张角色参考图。");
    return;
  }
  button.disabled = true;
  button.textContent = "正在识别角色…";
  try {
    const project = await api(
      `/api/projects/${state.project.id}/characters/${button.dataset.describeCharacter}/describe`,
      {
        method: "POST",
        body: JSON.stringify({ assetId }),
      },
    );
    renderProject(project, { preserveTab: true });
    showToast("已根据参考图更新角色设定。");
  } finally {
    button.disabled = false;
  }
}

function syncVideoReferenceLimit(form) {
  const inputs = [
    ...form.querySelectorAll('input[name="assetIds"]'),
  ];
  const selectedCount = inputs.filter((input) => input.checked).length;
  const counter = form.querySelector("[data-video-reference-count]");
  if (counter) {
    counter.textContent =
      `${selectedCount}/${MAX_VIDEO_REFERENCE_IMAGES}`;
  }
  inputs.forEach((input) => {
    input.disabled =
      !input.checked && selectedCount >= MAX_VIDEO_REFERENCE_IMAGES;
  });
}

async function generateAsset(button) {
  if (!state.config?.imageModelEnabled) {
    showToast("请先在 .env 配置图片模型。");
    return;
  }
  const confirmed = window.confirm("生成参考图会产生图片 API 费用，确认继续吗？");
  if (!confirmed) return;
  const operationKey = assetOperationKey(
    "generate",
    button.dataset.ownerType,
    button.dataset.ownerId,
  );
  const referenceKey = assetReferenceKey(
    state.project.id,
    button.dataset.ownerType,
    button.dataset.ownerId,
  );
  const referenceAssetIds = array(
    state.assetReferenceSelections[referenceKey],
  );
  const useGlobalReferences =
    Boolean(state.config?.imageEditModelEnabled) &&
    state.assetGlobalConsistency[referenceKey] !== false;
  const originalLabel = button.textContent;
  setAssetOperation(
    operationKey,
    "running",
    referenceAssetIds.length || useGlobalReferences
      ? "正在读取项目参考图并生成一致性图片…"
      : "正在生成，请稍候…",
  );
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    await api(`/api/projects/${state.project.id}/assets/generate`, {
      method: "POST",
      body: JSON.stringify({
        ownerType: button.dataset.ownerType,
        ownerId: button.dataset.ownerId,
        prompt: decodeURIComponent(button.dataset.prompt || ""),
        referenceAssetIds,
        useGlobalReferences,
        size:
          state.project.brief?.aspectRatio === "9:16"
            ? "1024x1536"
            : "1536x1024",
      }),
    });
    setAssetOperation(operationKey, "succeeded", "生成成功，图片已保存。");
    const project = await api(`/api/projects/${state.project.id}`);
    renderProject(project, { preserveTab: true });
    showToast("参考图已生成并保存。");
  } catch (error) {
    setAssetOperation(
      operationKey,
      "failed",
      `生成失败：${error.message}`,
    );
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function fileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("读取图片失败。"));
    reader.readAsDataURL(file);
  });
}

async function uploadAsset(input) {
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > 30 * 1024 * 1024) {
    showToast("图片不能超过 30 MB。");
    input.value = "";
    return;
  }
  try {
    const dataUrl = await fileAsDataUrl(file);
    await api(`/api/projects/${state.project.id}/assets/upload`, {
      method: "POST",
      body: JSON.stringify({
        ownerType: input.dataset.ownerType,
        ownerId: input.dataset.ownerId,
        dataUrl,
        fileName: file.name,
      }),
    });
    const project = await api(`/api/projects/${state.project.id}`);
    renderProject(project, { preserveTab: true });
    showToast("参考图已上传。");
  } catch (error) {
    showToast(error.message);
  } finally {
    input.value = "";
  }
}

async function uploadLibraryAsset(input) {
  const ownerSelect = elements.tabPanel.querySelector(
    "[data-library-owner]",
  );
  const [ownerType, ownerId] = String(ownerSelect?.value || "").split("|");
  if (!ownerType || !ownerId) {
    showToast("请先选择图片所属的角色或场景。");
    input.value = "";
    return;
  }
  input.dataset.ownerType = ownerType;
  input.dataset.ownerId = ownerId;
  await uploadAsset(input);
}

async function deleteImageAsset(assetId) {
  if (
    !window.confirm(
      "确定删除这张图片吗？相关角色、场景、分镜参考图和固定帧绑定也会同步移除，且无法恢复。",
    )
  ) {
    return;
  }
  try {
    await api(
      `/api/projects/${state.project.id}/assets/${encodeURIComponent(assetId)}`,
      { method: "DELETE" },
    );
    const project = await api(`/api/projects/${state.project.id}`);
    renderProject(project, { preserveTab: true });
    showToast("图片资产及其绑定已删除。");
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteVideoAsset(videoAssetId) {
  const label =
    videoAssetId === "final" ? "最终合片" : "这个视频镜头";
  if (
    !window.confirm(
      `确定删除${label}吗？本地文件和播放地址会被清理，镜头之后仍可重新生成。`,
    )
  ) {
    return;
  }
  try {
    const production = await api(
      `/api/projects/${state.project.id}/video-assets/${encodeURIComponent(videoAssetId)}`,
      { method: "DELETE" },
    );
    renderProject(
      {
        ...state.project,
        videoProduction: production,
      },
      { preserveTab: true },
    );
    showToast(`${label}已删除。`);
  } catch (error) {
    showToast(error.message);
  }
}

async function editAsset(button) {
  if (!state.config?.imageEditModelEnabled) {
    showToast("请先在 .env 配置图片编辑模型。");
    return;
  }
  const prompt = window.prompt(
    "描述要修改的内容。未明确提到的人物身份或场景结构会尽量保持不变：",
    "",
  );
  if (!prompt?.trim()) return;
  const operationKey = assetOperationKey(
    "edit",
    button.dataset.assetId,
  );
  const originalLabel = button.textContent;
  setAssetOperation(operationKey, "running", "正在修改，请稍候…");
  button.disabled = true;
  button.textContent = "修改中…";
  try {
    await api(`/api/projects/${state.project.id}/assets/edit`, {
      method: "POST",
      body: JSON.stringify({
        assetId: button.dataset.assetId,
        prompt: prompt.trim(),
        size:
          button.dataset.ownerType === "character"
            ? "1024x1536"
            : "1536x1024",
      }),
    });
    setAssetOperation(operationKey, "succeeded", "修改成功，已生成新版本。");
    const project = await api(`/api/projects/${state.project.id}`);
    renderProject(project, { preserveTab: true });
    showToast("图片修改完成，旧版本仍保留在素材库中。");
  } catch (error) {
    setAssetOperation(
      operationKey,
      "failed",
      `修改失败：${error.message}`,
    );
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function generateAllAssets(button, skipConfirmation = false) {
  if (!state.config?.imageModelEnabled) {
    showToast("请先在 .env 配置图片模型。");
    return;
  }
  const owners = [
    ...array(state.project.characters).map((item) => ({
      ownerType: "character",
      ownerId: item.id,
      prompt: item.imagePrompt,
      hasImage: array(item.referenceImageIds).length > 0,
    })),
    ...array(state.project.scenes).map((item) => ({
      ownerType: "scene",
      ownerId: item.id,
      prompt: item.imagePrompt,
      hasImage: array(item.referenceImageIds).length > 0,
    })),
  ].filter((item) => !item.hasImage);
  if (!owners.length) {
    showToast("角色和场景都已有参考图。");
    return;
  }
  if (
    !skipConfirmation &&
    !window.confirm(
      `将生成 ${owners.length} 张角色/场景参考图并产生图片 API 费用，确认继续吗？`,
    )
  ) {
    return;
  }
  button.disabled = true;
  const failures = [];
  try {
    for (let index = 0; index < owners.length; index += 1) {
      const owner = owners[index];
      const operationKey = assetOperationKey(
        "generate",
        owner.ownerType,
        owner.ownerId,
      );
      showToast(`正在生成参考图 ${index + 1}/${owners.length}…`);
      setAssetOperation(
        operationKey,
        "running",
        `正在生成 ${index + 1}/${owners.length}…`,
      );
      try {
        await api(`/api/projects/${state.project.id}/assets/generate`, {
          method: "POST",
          body: JSON.stringify({
            ...owner,
            referenceAssetIds: [],
            useGlobalReferences: Boolean(
              state.config?.imageEditModelEnabled,
            ),
            size:
              owner.ownerType === "character"
                ? "1024x1536"
                : "1536x1024",
          }),
        });
        setAssetOperation(
          operationKey,
          "succeeded",
          "生成成功，图片已保存。",
        );
      } catch (error) {
        failures.push(error);
        setAssetOperation(
          operationKey,
          "failed",
          `生成失败：${error.message}`,
        );
      }
    }
    const project = await api(`/api/projects/${state.project.id}`);
    renderProject(project, { preserveTab: true });
    showToast(
      failures.length
        ? `批量生成结束：${owners.length - failures.length} 个成功，${failures.length} 个失败。`
        : "角色与场景参考图已生成。",
    );
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function retrySingleVideoJob(jobId) {
  if (
    !window.confirm(
      "只重新生成这个视频分镜。该操作会再次产生视频 API 费用，确认继续吗？",
    )
  ) {
    return;
  }
  try {
    const production = await api(
      `/api/projects/${state.project.id}/video-production/jobs/${jobId}/retry`,
      { method: "POST", body: JSON.stringify({}) },
    );
    applyProductionUpdate({
      ...state.project,
      videoProduction: production,
    });
    showToast("该分镜已重新进入生成队列。");
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteCurrentProject() {
  if (!state.project) return;
  if (
    !window.confirm(
      `确定删除项目“${state.project.title}”吗？项目 JSON、图片和视频文件都会从本机删除，且无法恢复。`,
    )
  ) {
    return;
  }
  try {
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    window.clearTimeout(state.productionTimer);
    clearDeferredProductionRender();
    state.project = null;
    await loadProjects();
    elements.pageTitle.textContent = "把念头，驶向画面。";
    switchView("composer");
    showToast("项目已删除。");
  } catch (error) {
    showToast(error.message);
  }
}

async function handleGenerate(event) {
  event.preventDefault();
  const idea = $("#ideaInput").value.trim();
  if (!idea) {
    showToast("先写下一点什么吧。");
    $("#ideaInput").focus();
    return;
  }
  const payload = {
    mode: state.mode,
    idea,
    audience: $("#audienceInput").value,
    duration: Number($("#durationInput").value),
    aspectRatio: $("#aspectInput").value,
    style: $("#styleInput").value,
    tone: $("#toneInput").value,
    requirements: $("#requirementsInput").value,
  };
  switchView("loading");
  elements.generateButton.disabled = true;
  startLoadingMessages();
  try {
    const project = await api("/api/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderProject(project);
    await loadProjects();
    if ($("#autoAssetsInput").checked && state.config?.imageModelEnabled) {
      state.activeTab = "characters";
      renderActiveTab();
      await generateAllAssets(elements.generateButton, true);
    }
  } catch (error) {
    switchView("composer");
    showToast(error.message);
  } finally {
    stopLoadingMessages();
    elements.generateButton.disabled = false;
  }
}

function markdownFromProject(project) {
  const d = project.deliverables || {};
  const lines = [
    `# ${project.title}`,
    "",
    `- 模式：${project.modeLabel}`,
    `- 时长：${project.brief?.durationSeconds || ""} 秒`,
    `- 画幅：${project.brief?.aspectRatio || ""}`,
    "",
    "## 创意总览",
    "",
    `- 钩子：${project.brief?.hook || ""}`,
    `- 核心冲突：${project.brief?.coreConflict || ""}`,
    `- 目标受众：${project.brief?.audience || ""}`,
    `- 创作目标：${project.brief?.goal || ""}`,
    "",
    "## 剧本",
    "",
    project.script?.synopsis || "",
    "",
    "### 旁白 / 台词",
    "",
    project.script?.narration || "",
    "",
    "## 角色",
    "",
    ...array(project.characters).flatMap((character) => [
      `### ${character.name}｜${character.role}`,
      "",
      `- 视觉锚点：${character.visualIdentity}`,
      `- 性格：${character.personality}`,
      `- 声音：${character.voice}`,
      "",
    ]),
    "## 分镜",
    "",
    ...array(project.storyboard).flatMap((shot) => [
      `### 镜头 ${shot.shot}｜${shot.duration} 秒`,
      "",
      `- 场景：${shot.scene}`,
      `- 动作：${shot.action}`,
      `- 机位：${shot.camera}`,
      `- 台词：${shot.dialogue || "—"}`,
      `- 声音：${shot.audio}`,
      `- 连续性：${shot.continuity}`,
      `- 关键帧提示词：${shot.visualPrompt}`,
      `- 分镜运动提示词：${shot.videoPrompt}`,
      `- 已确认完整视频提示词：${shot.completeVideoPrompt || "尚未生成"}`,
      "",
    ]),
    "## 发布包",
    "",
    `- 发布文案：${d.caption || ""}`,
    `- 标签：${array(d.hashtags).join(" ")}`,
    `- 封面提示词：${d.coverPrompt || ""}`,
    `- 配乐提示词：${d.musicPrompt || ""}`,
    `- 负面提示词：${d.negativePrompt || ""}`,
  ];
  return lines.join("\n");
}

function downloadFile(name, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function safeFileName(title) {
  return title.replace(/[\\/:*?"<>|]/g, "-").slice(0, 60) || "jingzhou-project";
}

function bindEvents() {
  $$("#modeGrid .mode-card").forEach((button) => {
    button.addEventListener("click", () => {
      state.mode = button.dataset.mode;
      $$("#modeGrid .mode-card").forEach((item) => item.classList.toggle("active", item === button));
    });
  });

  elements.advancedToggle.addEventListener("click", () => {
    elements.advancedFields.classList.toggle("hidden");
    elements.advancedToggle.textContent = elements.advancedFields.classList.contains("hidden")
      ? "更多设定"
      : "收起设定";
  });

  elements.form.addEventListener("submit", handleGenerate);

  $("#newProjectButton").addEventListener("click", () => {
    window.clearTimeout(state.productionTimer);
    clearDeferredProductionRender();
    elements.pageTitle.textContent = "把念头，驶向画面。";
    switchView("composer");
    $("#ideaInput").focus();
  });

  elements.projectList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-project-id]");
    if (!button) return;
    try {
      renderProject(await api(`/api/projects/${button.dataset.projectId}`));
    } catch (error) {
      showToast(error.message);
    }
  });

  $("#resultTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (!button) return;
    clearDeferredProductionRender();
    state.activeTab = button.dataset.tab;
    renderActiveTab();
  });

  elements.tabPanel.addEventListener("click", async (event) => {
    const applyChatButton = event.target.closest("[data-chat-apply]");
    if (applyChatButton) {
      try {
        await resolveChatProposal(
          applyChatButton.dataset.chatApply,
          true,
        );
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const rejectChatButton = event.target.closest("[data-chat-reject]");
    if (rejectChatButton) {
      try {
        await resolveChatProposal(
          rejectChatButton.dataset.chatReject,
          false,
        );
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const describeButton = event.target.closest(
      "[data-describe-character]",
    );
    if (describeButton) {
      try {
        await describeCharacter(describeButton);
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const deleteShotButton = event.target.closest("[data-delete-shot]");
    if (deleteShotButton) {
      try {
        await deleteShot(deleteShotButton.dataset.deleteShot);
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const deleteCharacterButton = event.target.closest(
      "[data-delete-character]",
    );
    if (deleteCharacterButton) {
      try {
        await deleteIdentity(
          "character",
          deleteCharacterButton.dataset.deleteCharacter,
        );
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const deleteSceneButton = event.target.closest("[data-delete-scene]");
    if (deleteSceneButton) {
      try {
        await deleteIdentity("scene", deleteSceneButton.dataset.deleteScene);
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const previewButton = event.target.closest("[data-image-preview]");
    if (previewButton) {
      openImagePreview(
        previewButton.dataset.imagePreview,
        previewButton.dataset.imageLabel,
      );
      return;
    }
    const filterButton = event.target.closest(
      "[data-asset-library-filter]",
    );
    if (filterButton) {
      state.assetLibraryFilter =
        filterButton.dataset.assetLibraryFilter;
      renderActiveTab();
      return;
    }
    const deleteAssetButton = event.target.closest("[data-delete-asset]");
    if (deleteAssetButton) {
      await deleteImageAsset(deleteAssetButton.dataset.deleteAsset);
      return;
    }
    const deleteVideoButton = event.target.closest(
      "[data-delete-video-asset]",
    );
    if (deleteVideoButton) {
      await deleteVideoAsset(
        deleteVideoButton.dataset.deleteVideoAsset,
      );
      return;
    }
    const editAssetButton = event.target.closest("[data-edit-asset]");
    if (editAssetButton) {
      await editAsset(editAssetButton);
      return;
    }
    const generateAssetButton = event.target.closest("[data-generate-asset]");
    if (generateAssetButton) {
      await generateAsset(generateAssetButton);
      return;
    }
    const generateAllButton = event.target.closest("[data-generate-all-assets]");
    if (generateAllButton) {
      await generateAllAssets(generateAllButton);
      return;
    }
    const retryJobButton = event.target.closest("[data-retry-job]");
    if (retryJobButton) {
      await retrySingleVideoJob(retryJobButton.dataset.retryJob);
      return;
    }
    const generateShotButton = event.target.closest(
      "[data-generate-shot-video]",
    );
    if (generateShotButton) {
      await startShotVideo(generateShotButton.dataset.generateShotVideo);
      return;
    }
    const generatePromptButton = event.target.closest(
      "[data-generate-video-prompt]",
    );
    if (generatePromptButton) {
      await generateShotVideoPrompt(
        generatePromptButton.dataset.generateVideoPrompt,
        generatePromptButton,
      );
      return;
    }
    if (event.target.closest("[data-start-video]")) {
      await startVideoProduction();
      return;
    }
    if (event.target.closest("[data-refresh-production]")) {
      await refreshProduction();
      return;
    }
    if (event.target.closest("[data-retry-production]")) {
      await retryFailedProduction();
      return;
    }
    const assembleButton = event.target.closest(
      "[data-assemble-production]",
    );
    if (assembleButton) {
      await assembleProduction(assembleButton);
      return;
    }
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    await navigator.clipboard.writeText(decodeURIComponent(button.dataset.copy));
    showToast("提示词已复制。");
  });

  elements.tabPanel.addEventListener("submit", async (event) => {
    const editForm = event.target.closest("[data-edit-form]");
    const referenceForm = event.target.closest("[data-reference-form]");
    const chatForm = event.target.closest("[data-project-chat-form]");
    const addCharacterForm = event.target.closest(
      "[data-add-character-form]",
    );
    const addSceneForm = event.target.closest("[data-add-scene-form]");
    const addShotForm = event.target.closest("[data-add-shot-form]");
    if (
      !editForm &&
      !referenceForm &&
      !chatForm &&
      !addCharacterForm &&
      !addSceneForm &&
      !addShotForm
    ) return;
    event.preventDefault();
    try {
      if (editForm) await saveEditableField(editForm);
      if (referenceForm) await saveReferenceBinding(referenceForm);
      if (chatForm) await sendProjectChat(chatForm);
      if (addCharacterForm) await addManualCharacter(addCharacterForm);
      if (addSceneForm) await addManualScene(addSceneForm);
      if (addShotForm) await addManualShot(addShotForm);
    } catch (error) {
      showToast(error.message);
    }
  });

  elements.tabPanel.addEventListener("change", async (event) => {
    const videoReferenceInput = event.target.closest(
      "[data-video-reference]",
    );
    if (videoReferenceInput) {
      const form = videoReferenceInput.closest("[data-reference-form]");
      const selectedCount = form.querySelectorAll(
        'input[name="assetIds"]:checked',
      ).length;
      if (selectedCount > MAX_VIDEO_REFERENCE_IMAGES) {
        videoReferenceInput.checked = false;
        showToast(
          `每个分镜最多选择 ${MAX_VIDEO_REFERENCE_IMAGES} 张参考图。`,
        );
      }
      syncVideoReferenceLimit(form);
      return;
    }
    const libraryUpload = event.target.closest("[data-library-upload]");
    if (libraryUpload) {
      await uploadLibraryAsset(libraryUpload);
      return;
    }
    const referenceInput = event.target.closest(
      "[data-generation-asset-reference]",
    );
    if (referenceInput) {
      const key = assetReferenceKey(
        state.project.id,
        referenceInput.dataset.ownerType,
        referenceInput.dataset.ownerId,
      );
      const selected = new Set(array(state.assetReferenceSelections[key]));
      if (referenceInput.checked) {
        selected.add(referenceInput.value);
      } else {
        selected.delete(referenceInput.value);
      }
      state.assetReferenceSelections[key] = [...selected];
      renderActiveTab();
      return;
    }
    const globalInput = event.target.closest(
      "[data-global-asset-reference]",
    );
    if (globalInput) {
      const key = assetReferenceKey(
        state.project.id,
        globalInput.dataset.ownerType,
        globalInput.dataset.ownerId,
      );
      state.assetGlobalConsistency[key] = globalInput.checked;
      renderActiveTab();
      return;
    }
    const uploadInput = event.target.closest("[data-upload-asset]");
    if (uploadInput) await uploadAsset(uploadInput);
  });

  elements.produceVideo.addEventListener("click", () => {
    state.activeTab = "storyboard";
    renderActiveTab();
  });

  elements.downloadJson.addEventListener("click", () => {
    downloadFile(
      `${safeFileName(state.project.title)}.json`,
      JSON.stringify(state.project, null, 2),
      "application/json;charset=utf-8",
    );
  });

  elements.downloadMarkdown.addEventListener("click", () => {
    downloadFile(
      `${safeFileName(state.project.title)}-制作单.md`,
      markdownFromProject(state.project),
      "text/markdown;charset=utf-8",
    );
  });

  elements.deleteProject.addEventListener("click", deleteCurrentProject);

  elements.imagePreviewClose.addEventListener(
    "click",
    closeImagePreview,
  );
  elements.imagePreviewModal.addEventListener("click", (event) => {
    if (event.target === elements.imagePreviewModal) {
      closeImagePreview();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      !elements.imagePreviewModal.classList.contains("hidden")
    ) {
      closeImagePreview();
    }
  });
}

async function init() {
  bindEvents();
  try {
    await Promise.all([loadConfig(), loadProjects()]);
  } catch (error) {
    showToast(`初始化失败：${error.message}`);
  }
}

init();
