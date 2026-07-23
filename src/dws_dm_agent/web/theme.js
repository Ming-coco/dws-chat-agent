(function () {
  "use strict";

  const DESIGN_KEY = "dws-chat-agent-design-v1";
  const STORAGE_KEY = "dws-chat-agent-theme-v1";
  const CUSTOM_THEMES_KEY = "dws-chat-agent-custom-themes-v1";
  const HEX = /^#[0-9a-f]{6}$/i;
  const designs = [
    {
      id: "metropolis",
      name: "Metropolis",
      home: "/",
      note: "DIAMOND Network · 卡片式运营控制台"
    },
    {
      id: "diamond",
      name: "Diamond",
      home: "/diamond-preview",
      note: "DIAMOND Network · 战术拓扑与菱形几何"
    }
  ];
  const themes = [
    {
      id: "ice-signal",
      name: "Ice Signal",
      mode: "dark",
      background: "#060A0D",
      surface: "#111B22",
      accent: "#79C5EE",
      normal: "#52D6C2",
      attention: "#F0C35A",
      urgent: "#E55242",
      note: "黑色战术界面、冰蓝信号与克制红色状态"
    },
    {
      id: "ocean-signal",
      name: "Ocean Signal",
      mode: "dark",
      background: "#06151D",
      surface: "#102431",
      accent: "#4ED6C0",
      normal: "#56C596",
      attention: "#F5BD4F",
      urgent: "#E85332",
      note: "深海军蓝、青绿线路、琥珀与橙红站点"
    },
    {id:"dracula", name:"Dracula", mode:"dark", background:"#282A36", surface:"#44475A", accent:"#BD93F9", normal:"#50FA7B", attention:"#F1FA8C", urgent:"#FF5555", note:"高饱和开发者工具"},
    {id:"one-dark", name:"One Dark", mode:"dark", background:"#282C34", surface:"#21252B", accent:"#61AFEF", normal:"#98C379", attention:"#E5C07B", urgent:"#E06C75", note:"克制均衡的现代 IDE"},
    {id:"nord", name:"Nord", mode:"dark", background:"#2E3440", surface:"#3B4252", accent:"#88C0D0", normal:"#A3BE8C", attention:"#EBCB8B", urgent:"#BF616A", note:"冷静低饱和"},
    {id:"tokyo-night", name:"Tokyo Night", mode:"dark", background:"#1A1B26", surface:"#24283B", accent:"#7AA2F7", normal:"#9ECE6A", attention:"#E0AF68", urgent:"#F7768E", note:"深蓝夜景"},
    {id:"catppuccin-mocha", name:"Catppuccin Mocha", mode:"dark", background:"#1E1E2E", surface:"#313244", accent:"#CBA6F7", normal:"#A6E3A1", attention:"#F9E2AF", urgent:"#F38BA8", note:"柔和圆润"},
    {id:"gruvbox-dark", name:"Gruvbox Dark", mode:"dark", background:"#282828", surface:"#3C3836", accent:"#D79921", normal:"#B8BB26", attention:"#FE8019", urgent:"#FB4934", note:"复古暖色终端"},
    {id:"monokai", name:"Monokai", mode:"dark", background:"#272822", surface:"#3E3D32", accent:"#F92672", normal:"#A6E22E", attention:"#E6DB74", urgent:"#FD5F5F", note:"鲜艳高对比"},
    {id:"solarized-dark", name:"Solarized Dark", mode:"dark", background:"#002B36", surface:"#073642", accent:"#268BD2", normal:"#859900", attention:"#B58900", urgent:"#DC322F", note:"长时间阅读"},
    {id:"ayu-mirage", name:"Ayu Mirage", mode:"dark", background:"#1F2430", surface:"#242936", accent:"#FFCC66", normal:"#BAE67E", attention:"#FFA759", urgent:"#F07178", note:"暖灰与金黄"},
    {id:"material-dark", name:"Material Dark", mode:"dark", background:"#212121", surface:"#303030", accent:"#009688", normal:"#66BB6A", attention:"#FFB300", urgent:"#EF5350", note:"标准产品后台"},
    {id:"github-dark", name:"GitHub Dark", mode:"dark", background:"#0D1117", surface:"#161B22", accent:"#2F81F7", normal:"#3FB950", attention:"#D29922", urgent:"#F85149", note:"中性工程平台"},
    {id:"rose-pine", name:"Rosé Pine", mode:"dark", background:"#191724", surface:"#26233A", accent:"#C4A7E7", normal:"#9CCFD8", attention:"#F6C177", urgent:"#EB6F92", note:"柔和灰紫"},
    {id:"github-light", name:"GitHub Light", mode:"light", background:"#FFFFFF", surface:"#F6F8FA", accent:"#0969DA", normal:"#1A7F37", attention:"#9A6700", urgent:"#CF222E", note:"文档与代码平台"},
    {id:"solarized-light", name:"Solarized Light", mode:"light", background:"#FDF6E3", surface:"#EEE8D5", accent:"#268BD2", normal:"#6C7A00", attention:"#9A6B00", urgent:"#C3312F", note:"阅读与研究工具"},
    {id:"nord-light", name:"Nord Light", mode:"light", background:"#ECEFF4", surface:"#E5E9F0", accent:"#5E81AC", normal:"#47755A", attention:"#8A6500", urgent:"#A53F4B", note:"冷淡干净"},
    {id:"material-light", name:"Material Light", mode:"light", background:"#FAFAFA", surface:"#FFFFFF", accent:"#00796B", normal:"#2E7D32", attention:"#ED6C02", urgent:"#D32F2F", note:"通用管理系统"},
    {id:"ibm-carbon-light", name:"IBM Carbon Light", mode:"light", background:"#F4F4F4", surface:"#FFFFFF", accent:"#0F62FE", normal:"#198038", attention:"#8E6A00", urgent:"#DA1E28", note:"企业运维软件"},
    {id:"ant-design", name:"Ant Design", mode:"light", background:"#F5F5F5", surface:"#FFFFFF", accent:"#1677FF", normal:"#389E0D", attention:"#D48806", urgent:"#CF1322", note:"表单密集后台"},
    {id:"tailwind-slate", name:"Tailwind Slate", mode:"light", background:"#F8FAFC", surface:"#FFFFFF", accent:"#2563EB", normal:"#059669", attention:"#D97706", urgent:"#DC2626", note:"现代 SaaS"},
    {id:"warm-paper", name:"Warm Paper", mode:"light", background:"#F7F5F0", surface:"#FFFFFF", accent:"#2F6F68", normal:"#4F7A52", attention:"#A56700", urgent:"#B54732", note:"报告与研究终端"}
  ];

  function channels(hex) {
    const value = hex.slice(1);
    return [0, 2, 4].map(index => parseInt(value.slice(index, index + 2), 16));
  }

  function color(values) {
    return `#${values.map(value => Math.round(value).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
  }

  function mix(first, second, weight) {
    const left = channels(first);
    const right = channels(second);
    return color(left.map((value, index) => value + (right[index] - value) * weight));
  }

  function alpha(hex, opacity) {
    return `rgba(${channels(hex).join(",")},${opacity})`;
  }

  function luminance(hex) {
    const values = channels(hex).map(value => {
      const normalized = value / 255;
      return normalized <= 0.03928
        ? normalized / 12.92
        : Math.pow((normalized + 0.055) / 1.055, 2.4);
    });
    return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
  }

  function contrast(first, second) {
    const one = luminance(first);
    const two = luminance(second);
    return (Math.max(one, two) + 0.05) / (Math.min(one, two) + 0.05);
  }

  function readable(colorValue, background, text) {
    const backgrounds = Array.isArray(background) ? background : [background];
    for (let weight = 0; weight <= 1; weight += 0.05) {
      const candidate = mix(colorValue, text, weight);
      if (backgrounds.every(value => contrast(candidate, value) >= 4.6)) return candidate;
    }
    return text;
  }

  function validColor(value, fallback) {
    return HEX.test(String(value || "")) ? String(value).toUpperCase() : fallback;
  }

  function normalize(value) {
    const source = value || {};
    const background = validColor(source.background, "#06151D");
    const surface = validColor(source.surface, "#102431");
    const accent = validColor(source.accent, "#4ED6C0");
    const mode = source.mode === "light" || source.mode === "dark"
      ? source.mode
      : luminance(background) > 0.45 ? "light" : "dark";
    const normal = validColor(source.normal, mode === "dark" ? "#56C596" : "#147D64");
    const attention = validColor(source.attention, mode === "dark" ? "#F5BD4F" : "#9A6700");
    const urgent = validColor(source.urgent, mode === "dark" ? "#E85332" : "#B42318");
    return {
      id: String(source.id || "custom"),
      name: String(source.name || "自定义"),
      note: String(source.note || "自定义配色"),
      mode,
      background,
      surface,
      accent,
      normal,
      attention,
      urgent
    };
  }

  function storedCustomThemes() {
    try {
      const parsed = JSON.parse(localStorage.getItem(CUSTOM_THEMES_KEY) || "[]");
      if (!Array.isArray(parsed)) return [];
      const ids = new Set();
      return parsed.slice(0, 30).flatMap(item => {
        const value = normalize(item);
        if (!value.id.startsWith("custom-") || ids.has(value.id)) return [];
        ids.add(value.id);
        return [value];
      });
    } catch (error) {
      return [];
    }
  }

  function availableThemes() {
    return [...themes.map(normalize), ...storedCustomThemes()];
  }

  function tokens(theme) {
    const value = normalize(theme);
    const dark = value.mode === "dark";
    const text = dark ? "#F2F6F5" : "#1F2529";
    const panel2 = mix(value.surface, dark ? "#FFFFFF" : "#000000", dark ? 0.045 : 0.035);
    const toneOpacity = dark ? 0.12 : 0.08;
    const toneBackgrounds = tone => [
      value.surface,
      panel2,
      mix(value.surface, tone, toneOpacity),
      mix(panel2, tone, toneOpacity)
    ];
    const accentText = contrast(value.accent, "#101416") >= contrast(value.accent, "#F7FAF9")
      ? "#101416"
      : "#F7FAF9";
    return {
      "--bg": value.background,
      "--panel": value.surface,
      "--panel-2": panel2,
      "--control": mix(value.surface, value.background, 0.42),
      "--control-deep": mix(value.background, dark ? "#000000" : "#FFFFFF", dark ? 0.16 : 0.35),
      "--line": mix(value.surface, text, dark ? 0.16 : 0.18),
      "--line-strong": mix(value.surface, text, dark ? 0.28 : 0.3),
      "--text": text,
      "--text-secondary": mix(value.background, text, dark ? 0.82 : 0.78),
      "--muted": mix(value.background, text, dark ? 0.58 : 0.62),
      "--accent": value.accent,
      "--accent-ink": readable(value.accent, toneBackgrounds(value.accent), text),
      "--accent-text": accentText,
      "--accent-soft": alpha(value.accent, dark ? 0.1 : 0.08),
      "--accent-border": alpha(value.accent, dark ? 0.34 : 0.42),
      "--accent-2": value.normal,
      "--accent-2-ink": readable(value.normal, value.surface, text),
      "--accent-2-soft": alpha(value.normal, dark ? 0.09 : 0.07),
      "--accent-2-border": alpha(value.normal, 0.4),
      "--accent-3": value.attention,
      "--accent-3-ink": readable(value.attention, value.surface, text),
      "--accent-3-soft": alpha(value.attention, dark ? 0.09 : 0.07),
      "--accent-3-border": alpha(value.attention, 0.4),
      "--normal": value.normal,
      "--attention": value.attention,
      "--urgent": value.urgent,
      "--success": value.normal,
      "--success-ink": readable(value.normal, toneBackgrounds(value.normal), text),
      "--success-soft": alpha(value.normal, toneOpacity),
      "--success-border": alpha(value.normal, 0.42),
      "--info": value.accent,
      "--info-ink": readable(value.accent, toneBackgrounds(value.accent), text),
      "--info-soft": alpha(value.accent, toneOpacity),
      "--info-border": alpha(value.accent, 0.42),
      "--warning": value.attention,
      "--warning-ink": readable(value.attention, toneBackgrounds(value.attention), text),
      "--warning-soft": alpha(value.attention, toneOpacity),
      "--warning-border": alpha(value.attention, 0.38),
      "--danger": value.urgent,
      "--danger-ink": readable(value.urgent, toneBackgrounds(value.urgent), text),
      "--danger-soft": alpha(value.urgent, toneOpacity),
      "--danger-border": alpha(value.urgent, 0.4),
      "--lime": value.accent,
      "--cyan": value.accent,
      "--red": value.urgent,
      "--amber": value.attention,
      "--purple": value.accent
    };
  }

  function apply(theme) {
    const value = normalize(theme);
    const root = document.documentElement;
    Object.entries(tokens(value)).forEach(([name, token]) => root.style.setProperty(name, token));
    root.dataset.theme = value.id;
    root.dataset.color = value.id;
    root.dataset.colorMode = value.mode;
    root.style.colorScheme = value.mode;
    return value;
  }

  function storedSelection() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (parsed && parsed.id === "custom" && parsed.custom) {
        return {id:"custom", theme:normalize({...parsed.custom, id:"custom", name:"自定义"})};
      }
      const legacyId = {diamond:"ice-signal", metropolis:"ocean-signal"}[parsed?.id] || parsed?.id;
      const preset = availableThemes().find(item => item.id === legacyId);
      if (preset) return {id:preset.id, theme:normalize(preset)};
    } catch (error) {
      // A corrupt browser preference should never block the operations console.
    }
    return {id:"ocean-signal", theme:normalize(themes[1])};
  }

  function select(id) {
    const preset = availableThemes().find(item => item.id === id);
    if (!preset) throw new Error(`unknown theme: ${id}`);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({id}));
    const value = apply(preset);
    window.dispatchEvent(new CustomEvent("dws-theme-change", {detail:{id, theme:value}}));
    return value;
  }

  function selectCustom(value) {
    const custom = normalize({...value, id:"custom", name:"自定义"});
    localStorage.setItem(STORAGE_KEY, JSON.stringify({id:"custom", custom}));
    apply(custom);
    window.dispatchEvent(new CustomEvent("dws-theme-change", {detail:{id:"custom", theme:custom}}));
    return custom;
  }

  function saveCustom(name, value) {
    const cleanName = String(name || "").trim().slice(0, 40);
    if (!cleanName) throw new Error("请输入配色名称");
    const custom = normalize({
      ...value,
      id: `custom-${Date.now().toString(36)}`,
      name: cleanName,
      note: "用户创建"
    });
    const saved = [...storedCustomThemes(), custom].slice(-30);
    localStorage.setItem(CUSTOM_THEMES_KEY, JSON.stringify(saved));
    localStorage.setItem(STORAGE_KEY, JSON.stringify({id:custom.id}));
    apply(custom);
    window.dispatchEvent(new CustomEvent("dws-theme-change", {detail:{id:custom.id, theme:custom}}));
    return custom;
  }

  function removeCustom(id) {
    let selectedId = "";
    try {
      selectedId = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null")?.id || "";
    } catch (error) {
      selectedId = "";
    }
    const saved = storedCustomThemes().filter(item => item.id !== id);
    localStorage.setItem(CUSTOM_THEMES_KEY, JSON.stringify(saved));
    if (selectedId === id) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({id:"ocean-signal"}));
      return apply(themes[1]);
    }
    return storedSelection().theme;
  }

  function previewCustom(value) {
    return apply({...value, id:"custom-preview", name:"自定义预览"});
  }

  function currentDesign() {
    const stored = localStorage.getItem(DESIGN_KEY);
    return designs.find(item => item.id === stored) || designs[1];
  }

  function selectDesign(id) {
    const design = designs.find(item => item.id === id);
    if (!design) throw new Error(`unknown design: ${id}`);
    localStorage.setItem(DESIGN_KEY, design.id);
    document.documentElement.dataset.design = design.id;
    window.dispatchEvent(new CustomEvent("dws-design-change", {detail:{design}}));
    return design;
  }

  function syncDesignRoute(design) {
    const path = window.location.pathname;
    if (path === "/" && design.id === "diamond") {
      window.location.replace(`/diamond-preview${window.location.search}`);
    } else if ((path === "/diamond-preview" || path === "/diamond-preview.html") && design.id === "metropolis") {
      window.location.replace(`/${window.location.search}`);
    }
  }

  const initial = storedSelection();
  apply(initial.theme);
  const initialDesign = currentDesign();
  document.documentElement.dataset.design = initialDesign.id;
  window.DwsTheme = {
    designs,
    currentDesign,
    selectDesign,
    colors: themes.map(normalize),
    customColors: storedCustomThemes,
    currentColor: storedSelection,
    selectColor: select,
    saveCustomColor: saveCustom,
    removeCustomColor: removeCustom,
    previewCustomColor: previewCustom,
    themes: themes.map(normalize),
    customThemes: storedCustomThemes,
    current: storedSelection,
    select,
    selectCustom,
    saveCustom,
    removeCustom,
    previewCustom,
    normalize
  };
  syncDesignRoute(initialDesign);
})();
