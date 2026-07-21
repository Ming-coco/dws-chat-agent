(function () {
  "use strict";

  const STORAGE_KEY = "dws-chat-agent-theme-v1";
  const HEX = /^#[0-9a-f]{6}$/i;
  const themes = [
    {
      id: "metropolis",
      name: "Metropolis",
      mode: "dark",
      background: "#06151D",
      surface: "#102431",
      accent: "#4ED6C0",
      secondary: "#F5BD4F",
      tertiary: "#E85332",
      warning: "#F5BD4F",
      danger: "#E85332",
      note: "深海军蓝、青绿线路、琥珀与橙红站点"
    },
    {id:"dracula", name:"Dracula", mode:"dark", background:"#282A36", surface:"#44475A", accent:"#BD93F9", secondary:"#FF79C6", tertiary:"#8BE9FD", note:"高饱和开发者工具"},
    {id:"one-dark", name:"One Dark", mode:"dark", background:"#282C34", surface:"#21252B", accent:"#61AFEF", secondary:"#C678DD", tertiary:"#98C379", note:"克制均衡的现代 IDE"},
    {id:"nord", name:"Nord", mode:"dark", background:"#2E3440", surface:"#3B4252", accent:"#88C0D0", secondary:"#81A1C1", tertiary:"#A3BE8C", note:"冷静低饱和"},
    {id:"tokyo-night", name:"Tokyo Night", mode:"dark", background:"#1A1B26", surface:"#24283B", accent:"#7AA2F7", secondary:"#BB9AF7", tertiary:"#7DCFFF", note:"深蓝夜景"},
    {id:"catppuccin-mocha", name:"Catppuccin Mocha", mode:"dark", background:"#1E1E2E", surface:"#313244", accent:"#CBA6F7", secondary:"#89B4FA", tertiary:"#A6E3A1", note:"柔和圆润"},
    {id:"gruvbox-dark", name:"Gruvbox Dark", mode:"dark", background:"#282828", surface:"#3C3836", accent:"#D79921", secondary:"#83A598", tertiary:"#B8BB26", note:"复古暖色终端"},
    {id:"monokai", name:"Monokai", mode:"dark", background:"#272822", surface:"#3E3D32", accent:"#F92672", secondary:"#A6E22E", tertiary:"#66D9EF", note:"鲜艳高对比"},
    {id:"solarized-dark", name:"Solarized Dark", mode:"dark", background:"#002B36", surface:"#073642", accent:"#268BD2", secondary:"#2AA198", tertiary:"#B58900", note:"长时间阅读"},
    {id:"ayu-mirage", name:"Ayu Mirage", mode:"dark", background:"#1F2430", surface:"#242936", accent:"#FFCC66", secondary:"#5CCFE6", tertiary:"#F28779", note:"暖灰与金黄"},
    {id:"material-dark", name:"Material Dark", mode:"dark", background:"#212121", surface:"#303030", accent:"#009688", secondary:"#7C4DFF", tertiary:"#FF9800", note:"标准产品后台"},
    {id:"github-dark", name:"GitHub Dark", mode:"dark", background:"#0D1117", surface:"#161B22", accent:"#2F81F7", secondary:"#A371F7", tertiary:"#3FB950", note:"中性工程平台"},
    {id:"rose-pine", name:"Rosé Pine", mode:"dark", background:"#191724", surface:"#26233A", accent:"#C4A7E7", secondary:"#EBBCBA", tertiary:"#9CCFD8", note:"柔和灰紫"},
    {id:"github-light", name:"GitHub Light", mode:"light", background:"#FFFFFF", surface:"#F6F8FA", accent:"#0969DA", secondary:"#8250DF", tertiary:"#1A7F37", note:"文档与代码平台"},
    {id:"solarized-light", name:"Solarized Light", mode:"light", background:"#FDF6E3", surface:"#EEE8D5", accent:"#268BD2", secondary:"#2AA198", tertiary:"#B58900", note:"阅读与研究工具"},
    {id:"nord-light", name:"Nord Light", mode:"light", background:"#ECEFF4", surface:"#E5E9F0", accent:"#5E81AC", secondary:"#8FBCBB", tertiary:"#A3BE8C", note:"冷淡干净"},
    {id:"material-light", name:"Material Light", mode:"light", background:"#FAFAFA", surface:"#FFFFFF", accent:"#009688", secondary:"#673AB7", tertiary:"#F57C00", note:"通用管理系统"},
    {id:"ibm-carbon-light", name:"IBM Carbon Light", mode:"light", background:"#F4F4F4", surface:"#FFFFFF", accent:"#0F62FE", secondary:"#8A3FFC", tertiary:"#198038", note:"企业运维软件"},
    {id:"ant-design", name:"Ant Design", mode:"light", background:"#F5F5F5", surface:"#FFFFFF", accent:"#1677FF", secondary:"#722ED1", tertiary:"#389E0D", note:"表单密集后台"},
    {id:"tailwind-slate", name:"Tailwind Slate", mode:"light", background:"#F8FAFC", surface:"#FFFFFF", accent:"#2563EB", secondary:"#7C3AED", tertiary:"#059669", note:"现代 SaaS"},
    {id:"warm-paper", name:"Warm Paper", mode:"light", background:"#F7F5F0", surface:"#FFFFFF", accent:"#2F6F68", secondary:"#A35D3A", tertiary:"#C18C2F", note:"报告与研究终端"}
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
    for (let weight = 0; weight <= 1; weight += 0.05) {
      const candidate = mix(colorValue, text, weight);
      if (contrast(candidate, background) >= 4.5) return candidate;
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
    const secondary = validColor(source.secondary, "#F5BD4F");
    const tertiary = validColor(source.tertiary, "#E85332");
    const mode = source.mode === "light" || source.mode === "dark"
      ? source.mode
      : luminance(background) > 0.45 ? "light" : "dark";
    return {
      id: String(source.id || "custom"),
      name: String(source.name || "自定义"),
      note: String(source.note || "自定义配色"),
      mode,
      background,
      surface,
      accent,
      secondary,
      tertiary,
      warning: validColor(source.warning, mode === "dark" ? "#F0B84B" : "#9A6700"),
      danger: validColor(source.danger, mode === "dark" ? "#FF7168" : "#B42318")
    };
  }

  function tokens(theme) {
    const value = normalize(theme);
    const dark = value.mode === "dark";
    const text = dark ? "#F2F6F5" : "#1F2529";
    const success = dark ? "#49D3A5" : "#147D64";
    const info = dark ? "#79A9FF" : "#175CD3";
    const accentText = contrast(value.accent, "#101416") >= contrast(value.accent, "#F7FAF9")
      ? "#101416"
      : "#F7FAF9";
    return {
      "--bg": value.background,
      "--panel": value.surface,
      "--panel-2": mix(value.surface, dark ? "#FFFFFF" : "#000000", dark ? 0.045 : 0.035),
      "--control": mix(value.surface, value.background, 0.42),
      "--control-deep": mix(value.background, dark ? "#000000" : "#FFFFFF", dark ? 0.16 : 0.35),
      "--line": mix(value.surface, text, dark ? 0.16 : 0.18),
      "--line-strong": mix(value.surface, text, dark ? 0.28 : 0.3),
      "--text": text,
      "--text-secondary": mix(value.background, text, dark ? 0.82 : 0.78),
      "--muted": mix(value.background, text, dark ? 0.58 : 0.62),
      "--accent": value.accent,
      "--accent-ink": readable(value.accent, value.surface, text),
      "--accent-text": accentText,
      "--accent-soft": alpha(value.accent, dark ? 0.1 : 0.08),
      "--accent-border": alpha(value.accent, dark ? 0.34 : 0.42),
      "--accent-2": value.secondary,
      "--accent-2-ink": readable(value.secondary, value.surface, text),
      "--accent-2-soft": alpha(value.secondary, dark ? 0.09 : 0.07),
      "--accent-2-border": alpha(value.secondary, 0.4),
      "--accent-3": value.tertiary,
      "--accent-3-ink": readable(value.tertiary, value.surface, text),
      "--accent-3-soft": alpha(value.tertiary, dark ? 0.09 : 0.07),
      "--accent-3-border": alpha(value.tertiary, 0.4),
      "--success": success,
      "--success-ink": readable(success, value.surface, text),
      "--success-soft": alpha(success, dark ? 0.12 : 0.08),
      "--success-border": alpha(success, 0.42),
      "--info": info,
      "--info-ink": readable(info, value.surface, text),
      "--info-soft": alpha(info, dark ? 0.12 : 0.08),
      "--info-border": alpha(info, 0.42),
      "--warning": value.warning,
      "--warning-ink": readable(value.warning, value.surface, text),
      "--warning-soft": alpha(value.warning, dark ? 0.12 : 0.08),
      "--warning-border": alpha(value.warning, 0.38),
      "--danger": value.danger,
      "--danger-ink": readable(value.danger, value.surface, text),
      "--danger-soft": alpha(value.danger, 0.12),
      "--danger-border": alpha(value.danger, 0.4),
      "--lime": value.accent,
      "--cyan": value.accent,
      "--red": value.danger,
      "--amber": value.warning,
      "--purple": value.accent
    };
  }

  function apply(theme) {
    const value = normalize(theme);
    const root = document.documentElement;
    Object.entries(tokens(value)).forEach(([name, token]) => root.style.setProperty(name, token));
    root.dataset.theme = value.id;
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
      const preset = themes.find(item => item.id === parsed?.id);
      if (preset) return {id:preset.id, theme:normalize(preset)};
    } catch (error) {
      // A corrupt browser preference should never block the operations console.
    }
    return {id:"metropolis", theme:normalize(themes[0])};
  }

  function select(id) {
    const preset = themes.find(item => item.id === id);
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

  function previewCustom(value) {
    return apply({...value, id:"custom-preview", name:"自定义预览"});
  }

  const initial = storedSelection();
  apply(initial.theme);
  window.DwsTheme = {
    themes: themes.map(normalize),
    current: storedSelection,
    select,
    selectCustom,
    previewCustom,
    normalize
  };
})();
