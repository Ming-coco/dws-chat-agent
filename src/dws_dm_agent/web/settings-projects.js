/**
 * 设置页的「项目配置」区域。
 *
 * 独立成文件，不改动 settings.html 里已有逻辑。
 * 数据来源：
 *   GET  /api/projects              工作区里的项目目录
 *   GET  /api/project-config?name=  单个项目已保存的配置（密码是掩码）
 *   POST /api/project-config        保存
 *
 * 密码：后端返回掩码，前端也显示掩码。用户不动它就不会覆盖原值，
 * 动了才发新值——这样不会因为打开页面再保存就把密码清空。
 */
(function () {
  "use strict";

  const MASK = "********";

  const panel = document.getElementById("projectConfigPanel");
  if (!panel) return;

  // 自定义下拉：页面里已有 .project-picker / .project-menu 样式，
  // 原生 datalist 的弹层由浏览器绘制，跟深色主题完全不搭。
  const pickerBox = document.getElementById("pcProjectPicker");
  const pickerToggle = document.getElementById("pcProjectToggle");
  const pickerMenu = document.getElementById("pcProjectMenu");
  const pickerFilter = document.getElementById("pcProjectFilter");
  const pickerOptions = document.getElementById("pcProjectOptions");
  const currentLabel = document.getElementById("pcProjectCurrent");
  const form = document.getElementById("projectForm");
  const message = document.getElementById("pcMessage");
  const saveState = document.getElementById("projectSaveState");
  const rowTemplate = document.getElementById("pcDatabaseRow");
  const databasesBox = document.getElementById("pcDatabases");

  const fields = {
    frontend: document.getElementById("pcFrontend"),
    note: document.getElementById("pcNote"),
    loginUser: document.getElementById("pcLoginUser"),
    loginPassword: document.getElementById("pcLoginPassword"),
    serverHost: document.getElementById("pcServerHost"),
    serverPort: document.getElementById("pcServerPort"),
    serverUser: document.getElementById("pcServerUser"),
    serverPassword: document.getElementById("pcServerPassword"),
    deployDir: document.getElementById("pcDeployDir"),
  };

  let currentProject = "";
  let allProjects = [];
  let configured = new Set();

  /** 按关键字重建候选项。返回匹配数量。 */
  function renderOptions() {
    const keyword = (pickerFilter.value || "").trim().toLowerCase();
    const matched = allProjects.filter(
      (name) => !keyword || name.toLowerCase().includes(keyword)
    );

    pickerOptions.innerHTML = "";
    if (!matched.length) {
      const empty = document.createElement("div");
      empty.className = "project-empty";
      empty.textContent = keyword ? "没有匹配的项目" : "工作区里没有项目目录";
      pickerOptions.appendChild(empty);
      return 0;
    }

    matched.forEach((name) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "project-option" + (name === currentProject ? " on" : "");
      button.dataset.project = name;
      const tick = document.createElement("span");
      tick.className = "tick";
      // 已配过的用实心点，一眼看出哪些还没填
      tick.textContent = name === currentProject ? "✓" : configured.has(name) ? "●" : "";
      const label = document.createElement("span");
      label.textContent = name;
      button.append(tick, label);
      button.addEventListener("click", () => {
        closeMenu();
        loadProject(name);
      });
      pickerOptions.appendChild(button);
    });
    return matched.length;
  }

  function openMenu() {
    pickerMenu.hidden = false;
    pickerFilter.value = "";
    renderOptions();
    pickerFilter.focus();
  }

  function closeMenu() {
    pickerMenu.hidden = true;
  }

  function toggleMenu() {
    if (pickerMenu.hidden) openMenu();
    else closeMenu();
  }

  /** 更新「当前项目」那一行文字。 */
  function refreshCurrentLabel() {
    if (!currentProject) {
      currentLabel.textContent = "未选择";
      currentLabel.className = "project-empty";
      return;
    }
    currentLabel.textContent = currentProject;
    currentLabel.className = "project-chip primary";
  }

  /**
   * 弹出提示。设置页里的 toast 定义在另一个脚本的闭包里，这里访问不到，
   * 所以自己实现一个：固定在右上角、几秒后自动消失。
   */
  let toastTimer = null;
  function showToast(text, kind) {
    let box = document.getElementById("pcToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "pcToast";
      // 贴住内容区右上角而不是屏幕边缘：宽屏下内容居中，两侧留白很大，
      // 固定在屏幕右侧会离表单很远，看着像无关的浮层。
      box.style.cssText = [
        "position:fixed", "top:92px", "z-index:9999",
        "padding:10px 16px", "border-radius:10px", "font-size:13px",
        "border:1px solid var(--line-strong)", "background:var(--panel)",
        "box-shadow:0 14px 32px rgb(0 0 0 / .35)", "transition:opacity .2s",
        "max-width:360px", "pointer-events:none",
      ].join(";");
      document.body.appendChild(box);
    }
    box.textContent = text;
    // 成功用 success 色系、失败用 danger 色系，光用 accent 在深色底上太暗，
    // 看着像禁用态。这里连背景一起给，和页面里的 .badge 保持一致。
    // 不用主题变量：success-ink / danger-ink 在深色主题下偏灰，
    // 提示挂在深色面板上几乎看不清。这里直接给明确的颜色。
    const palette =
      kind === "error"
        ? { fg: "#ff8f8f", bg: "rgba(80, 24, 24, .96)", bd: "#c8524f" }
        : { fg: "#6ee7a8", bg: "rgba(18, 58, 40, .96)", bd: "#2f9e6b" };
    box.style.color = palette.fg;
    box.style.background = palette.bg;
    box.style.borderColor = palette.bd;
    box.style.fontWeight = "500";

    // 对齐到主内容区的右边缘；拿不到就退回屏幕右侧
    const main = document.querySelector("main");
    const margin = 16;
    if (main) {
      const rect = main.getBoundingClientRect();
      if (rect.width > 320) {
        box.style.left = "auto";
        box.style.right = Math.max(margin, window.innerWidth - rect.right + margin) + "px";
      } else {
        box.style.right = margin + "px";
        box.style.left = "auto";
      }
    } else {
      box.style.right = margin + "px";
      box.style.left = "auto";
    }
    box.style.opacity = "1";
    box.hidden = false;

    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      box.style.opacity = "0";
      setTimeout(() => { box.hidden = true; }, 220);
    }, kind === "error" ? 4000 : 2400);
  }

  /** 在面板底部显示一条状态信息。 */
  function setMessage(text, kind) {
    message.textContent = text || "";
    message.className = "message" + (kind ? " " + kind : "");
  }

  function addDatabaseRow(data) {
    const fragment = rowTemplate.content.cloneNode(true);
    const row = fragment.querySelector(".project-db-row");
    const values = data || {};
    row.querySelectorAll("[data-db]").forEach((input) => {
      const key = input.dataset.db;
      if (key === "password") {
        input.value = values.password ? MASK : "";
        input.placeholder = values.password ? "已设置，留空表示不修改" : "未设置";
      } else if (key === "type") {
        input.value = values[key] || "mysql";
      } else if (values[key] !== undefined && values[key] !== null) {
        input.value = values[key];
      }
    });
    row.querySelector(".pc-db-remove").addEventListener("click", () => row.remove());
    databasesBox.appendChild(row);
  }

  function collect() {
    const databases = [];
    databasesBox.querySelectorAll(".project-db-row").forEach((row) => {
      const item = {};
      row.querySelectorAll("[data-db]").forEach((input) => {
        item[input.dataset.db] = input.value.trim();
      });
      // 什么都没填的空行跳过，避免存一堆空记录
      if (!item.host && !item.schema && !item.name) return;
      item.port = Number(item.port) || 3306;
      databases.push(item);
    });

    return {
      name: currentProject,
      frontend: fields.frontend.value.trim(),
      note: fields.note.value.trim(),
      login_user: fields.loginUser.value.trim(),
      login_password: fields.loginPassword.value,
      server: {
        host: fields.serverHost.value.trim(),
        port: Number(fields.serverPort.value) || 22,
        user: fields.serverUser.value.trim(),
        password: fields.serverPassword.value,
        deployDir: fields.deployDir.value.trim(),
      },
      databases,
    };
  }

  function fill(config) {
    fields.frontend.value = config.frontend || "";
    fields.note.value = config.note || "";
    fields.loginUser.value = config.login_user || "";
    // 登录密码同样是掩码占位，不回显真实值
    fields.loginPassword.value = config.login_password ? MASK : "";
    fields.loginPassword.placeholder = config.login_password
      ? "已设置，留空表示不修改"
      : "未设置";
    const server = config.server || {};
    fields.serverHost.value = server.host || "";
    fields.serverPort.value = server.port || 22;
    fields.serverUser.value = server.user || "";
    // 有密码时显示掩码占位，不显示真实值
    fields.serverPassword.value = server.password ? MASK : "";
    fields.serverPassword.placeholder = server.password ? "已设置，留空表示不修改" : "未设置";
    fields.deployDir.value = server.deployDir || "";

    databasesBox.innerHTML = "";
    (config.databases || []).forEach((database) => addDatabaseRow(database));
    if (!(config.databases || []).length) addDatabaseRow();
  }

  async function loadProject(name, keepMessage) {
    currentProject = name;
    if (!keepMessage) setMessage("正在读取…", "");
    try {
      const response = await fetch(
        "/api/project-config?name=" + encodeURIComponent(name),
        { cache: "no-store" }
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
      fill(data.config || {});
      form.hidden = false;
      refreshCurrentLabel();
      saveState.textContent = data.saved ? "已配置" : "未配置";
      if (data.saved) configured.add(name);
      setMessage("", "");
    } catch (error) {
      setMessage("读取失败：" + error.message, "error");
    }
  }

  async function save(button) {
    if (!currentProject) {
      setMessage("请先选择项目", "error");
      return;
    }
    const payload = collect();
    // 登录账号也算有效配置：光有前端地址 + 账号就能跑页面检查，
    // 缺了它会让「只填了账号」的保存被静默拦住。
    if (
      !payload.server.host &&
      !payload.frontend &&
      !payload.login_user &&
      !payload.databases.length
    ) {
      setMessage("至少填一项：前端地址、登录账号、服务器或数据库", "error");
      return;
    }

    // 保存过程中禁用按钮，避免连点重复提交
    const original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "保存中…";
    }
    setMessage("正在保存…", "");

    try {
      const response = await fetch("/api/project-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
      saveState.textContent = "已配置";
      configured.add(currentProject);
      setMessage("已保存", "ok");
      // 顶部的 toast 比底部小字显眼
      showToast("项目配置已保存", "ok");
      // 重新读一次，让密码变成掩码
      await loadProject(currentProject, true);
      setMessage("已保存", "ok");
    } catch (error) {
      setMessage("保存失败：" + error.message, "error");
      showToast("保存失败：" + error.message, "error");
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  async function boot() {
    try {
      const response = await fetch("/api/projects", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      const projects = data.projects || [];
      if (!projects.length) return;

      console.log("[pc] 读到项目数:", projects.length);
      allProjects = projects;
      panel.hidden = false;
      refreshCurrentLabel();

      pickerToggle.addEventListener("click", toggleMenu);
      pickerFilter.addEventListener("input", renderOptions);
      pickerFilter.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
      });
      // 点菜单外面关掉
      document.addEventListener("click", (event) => {
        if (pickerMenu.hidden) return;
        if (pickerBox.contains(event.target)) return;
        closeMenu();
      });

      const addButton = document.getElementById("pcAddDatabase");
      if (addButton) addButton.addEventListener("click", () => {
        addDatabaseRow();
        // 新行插在末尾，滚过去让对方看到确实加上了
        const last = databasesBox.lastElementChild;
        if (last) last.scrollIntoView({block: "nearest"});
      });

      const saveButton = document.getElementById("pcSave");
      if (saveButton) saveButton.addEventListener("click", () => save(saveButton));
    } catch (error) {
      console.error("[pc] boot 失败:", error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
