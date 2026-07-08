// 翻译 / 简繁 / 中日双语 层。
// 数据来源：dotabyss-translation（官方繁体 zh_Hant，经服务器 /translations/ 映射）
// + 服务器 /api/translate（OpenAI 兼容 LLM 代理，补翻未收录句，落盘缓存）。
// 唯一渲染入口是 app.js 的 NovelModelMessage.show()，本模块提供 renderText/renderSpeaker。

const LS_KEY = 'dotabyss.i18n.settings.v1';
const DEFAULTS = {
  lang: 'zh_Hans',      // zh_Hans（简体，opencc 实时转） | zh_Hant（繁体原文）
  layout: 'zh',         // zh（仅中文） | bilingual（中日双语） | ja（仅日文原文）
  translateNames: true, // 角色名是否用 names 字典翻译
  aiMark: true,         // LLM 译文是否显示 ⚡ 标记
  playerName: '',       // 主角占位符 <user> 映射为该名字；留空回退「司令官」
};

const PLAYER_NAME_FALLBACK = '司令官';

const TEXT_COMMANDS = new Set([
  'message', 'l2dmessage', 'messagetextcenter', 'messagetextunder', 'dotmessage', 'title',
]);

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

// 与 app.js show() 一致：转义后仅还原 <br>，其余控制码按字面保留（既有行为）。
function textToHtml(text) {
  return escapeHtml(text || '').replace(/&lt;br&gt;/g, '<br>');
}

async function fetchJsonOpt(url, fallback) {
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return fallback;
    return await r.json();
  } catch (_) {
    return fallback;
  }
}

function loadLS() {
  try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; } catch (_) { return {}; }
}
function saveLS(s) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(s)); } catch (_) { /* ignore */ }
}

class Translator {
  constructor() {
    this.settings = { ...DEFAULTS, ...loadLS() };
    this.officialMap = {};   // 当前 story 官方译文 日→繁
    this.llmMap = {};        // 当前 story LLM 译文 日→繁
    this.namesMap = {};      // 全局角色名 日→繁
    this.currentStoryId = null;
    this.prefetching = false;
    this.llmEnabled = false;
    this.llmModel = '';
    this._converter = null;  // opencc 繁(tw)→简(cn)
    this._changeCbs = [];
  }

  async init() {
    try {
      if (window.OpenCC) this._converter = window.OpenCC.Converter({ from: 'tw', to: 'cn' });
    } catch (e) { console.warn('[i18n] opencc 初始化失败', e); }
    this.namesMap = await fetchJsonOpt('./translations/names/zh_Hant.json', {});
    const cfg = await fetchJsonOpt('./api/llm-config', { enabled: false });
    this.llmEnabled = !!cfg.enabled;
    this.llmModel = cfg.model || '';
  }

  onChange(cb) { this._changeCbs.push(cb); }
  _emit(reason) { for (const cb of this._changeCbs) { try { cb(reason); } catch (_) { /* ignore */ } } }

  setSetting(key, value) {
    this.settings[key] = value;
    saveLS(this.settings);
    this._emit('setting');
  }
  getSettings() { return { ...this.settings }; }
  importSettings(obj) {
    this.settings = { ...DEFAULTS, ...(obj || {}) };
    saveLS(this.settings);
    this._emit('setting');
  }

  _toSimplified(text) {
    if (this.settings.lang === 'zh_Hans' && this._converter && text) {
      try { return this._converter(text); } catch (_) { return text; }
    }
    return text;
  }

  // 主角占位符 <user> → 用户设定名（留空回退「司令官」）。
  // 必须在译文查表之后、textToHtml 之前对纯文本调用（<user> 是查表 key 的一部分，
  // 且经 textToHtml 会被转义成 &lt;user&gt;）。中日文与官方/AI 译文均含 <user>。
  applyPlayerName(text) {
    if (text == null) return text;
    const name = (this.settings.playerName || '').trim() || PLAYER_NAME_FALLBACK;
    return String(text).replace(/<user>/g, name);
  }

  // 加载当前 story 的官方译文，并在后台预取未收录句（LLM）。
  // 返回的 Promise 在官方译文就绪时 resolve；预取完成后再 _emit('prefetch') 触发重渲染。
  async loadStoryTranslations(storyId, script) {
    this.currentStoryId = storyId;
    this.officialMap = {};
    this.llmMap = {};
    this.officialMap = await fetchJsonOpt(
      `./translations/novels/${encodeURIComponent(storyId)}/zh_Hant.json`, {});
    this._prefetchLLM(storyId, script).then(() => this._emit('prefetch'));
    return this.officialMap;
  }

  _collectFromScript(script) {
    const texts = [];
    const speakers = new Set();
    for (const c of (script?.commands || [])) {
      const base = String(c.command || '').replace(/^async/, '');
      if (!TEXT_COMMANDS.has(base)) continue;
      if (c.message) texts.push(c.message);
      if (c.speaker) speakers.add(c.speaker);
    }
    return { texts, speakers: [...speakers] };
  }

  async _prefetchLLM(storyId, script) {
    const { texts, speakers } = this._collectFromScript(script);
    const uniq = [...new Set(texts)];
    const missing = uniq.filter((t) => !(t in this.officialMap));
    if (!missing.length) return;
    const namesSub = {};
    for (const s of speakers) if (this.namesMap[s]) namesSub[s] = this.namesMap[s];
    this.prefetching = true;
    this._emit('prefetch-start');
    try {
      const res = await fetch('./api/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prefixed_id: storyId, items: missing, context: { script: uniq, names: namesSub } }),
      });
      if (res.ok) {
        const j = await res.json();
        Object.assign(this.llmMap, j.translations || {});
        this.llmEnabled = !!j.llm_enabled;
      }
    } catch (e) {
      console.warn('[i18n] LLM 预取失败', e);
    } finally {
      this.prefetching = false;
    }
  }

  _lookup(rawText) {
    if (rawText in this.officialMap) return { zh: this.officialMap[rawText], isLLM: false, ok: true };
    if (rawText in this.llmMap) return { zh: this.llmMap[rawText], isLLM: true, ok: true };
    return { zh: null, isLLM: false, ok: false };
  }

  renderSpeaker(rawSpeaker) {
    if (!rawSpeaker) return '';
    if (this.settings.layout === 'ja') return textToHtml(this.applyPlayerName(rawSpeaker));
    let name = rawSpeaker;
    if (this.settings.translateNames && this.namesMap[rawSpeaker]) {
      name = this._toSimplified(this.namesMap[rawSpeaker]);
    }
    return textToHtml(this.applyPlayerName(name));
  }

  // 返回 { html }
  renderText(rawText) {
    const raw = rawText || '';
    const layout = this.settings.layout;
    if (layout === 'ja') {
      return { html: `<div class="tl-primary tl-ja">${textToHtml(this.applyPlayerName(raw))}</div>` };
    }
    const { zh, isLLM, ok } = this._lookup(raw);
    if (ok) {
      const disp = this._toSimplified(zh);
      const mark = (isLLM && this.settings.aiMark)
        ? '<span class="tl-ai" title="AI 翻译（非官方）">⚡</span>' : '';
      let html = `<div class="tl-primary">${textToHtml(this.applyPlayerName(disp))}${mark}</div>`;
      if (layout === 'bilingual') html += `<div class="tl-original">${textToHtml(this.applyPlayerName(raw))}</div>`;
      return { html };
    }
    // 未命中官方且 LLM 未就绪/失败/未配置 → 原文兜底
    let note = '';
    if (this.prefetching) note = '<span class="tl-pending">翻译中…</span>';
    else if (!this.llmEnabled) note = '<span class="tl-pending" title="未配置 config/llm.json">未译</span>';
    return { html: `<div class="tl-primary tl-untranslated">${textToHtml(this.applyPlayerName(raw))}${note}</div>` };
  }

  // -------- 设置面板 UI（自建 DOM 注入 body） --------
  mountSettingsPanel() {
    if (document.getElementById('i18nSettingsBtn')) return;
    const btn = document.createElement('button');
    btn.id = 'i18nSettingsBtn';
    btn.className = 'i18n-fab';
    btn.title = '翻译设置';
    btn.textContent = '译';
    document.body.appendChild(btn);

    const panel = document.createElement('div');
    panel.id = 'i18nPanel';
    panel.className = 'i18n-panel hidden';
    panel.innerHTML = this._panelHtml();
    document.body.appendChild(panel);

    btn.addEventListener('click', () => panel.classList.toggle('hidden'));
    panel.querySelector('.i18n-close').addEventListener('click', () => panel.classList.add('hidden'));

    panel.querySelectorAll('input[name="i18n-lang"]').forEach((r) =>
      r.addEventListener('change', () => this.setSetting('lang', r.value)));
    panel.querySelectorAll('input[name="i18n-layout"]').forEach((r) =>
      r.addEventListener('change', () => this.setSetting('layout', r.value)));
    panel.querySelector('#i18n-names').addEventListener('change', (e) =>
      this.setSetting('translateNames', e.target.checked));
    panel.querySelector('#i18n-aimark').addEventListener('change', (e) =>
      this.setSetting('aiMark', e.target.checked));
    panel.querySelector('#i18n-playername').addEventListener('input', (e) =>
      this.setSetting('playerName', e.target.value));

    const llmSave = panel.querySelector('#i18n-llm-save');
    if (llmSave) {
      llmSave.addEventListener('click', async () => {
        const note = panel.querySelector('.i18n-llm-save-note');
        const body = {};
        const key = panel.querySelector('#i18n-llm-key').value.trim();
        const model = panel.querySelector('#i18n-llm-model').value.trim();
        const baseUrl = panel.querySelector('#i18n-llm-baseurl').value.trim();
        if (key) body.api_key = key;
        if (model) body.model = model;
        if (baseUrl) body.base_url = baseUrl;
        note.textContent = '保存中…';
        try {
          const res = await fetch('./api/llm-config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          });
          const j = await res.json();
          this.llmEnabled = !!j.enabled;
          if (model) this.llmModel = model;
          panel.querySelector('.i18n-llm-status').textContent = this.llmEnabled ? `已启用：${model || this.llmModel || '?'}` : '未配置';
          note.textContent = this.llmEnabled ? '已启用 ✓' : '已保存';
          panel.querySelector('#i18n-llm-key').value = '';
        } catch (err) {
          note.textContent = '保存失败：' + err.message;
        }
      });
    }

    panel.querySelector('#i18n-export').addEventListener('click', () => this._exportConfig());
    const importInput = panel.querySelector('#i18n-import-file');
    panel.querySelector('#i18n-import').addEventListener('click', () => importInput.click());
    importInput.addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const obj = JSON.parse(await file.text());
        this.importSettings(obj.settings || obj);
        this._syncPanel(panel);
      } catch (err) { alert('导入失败：' + err.message); }
    });

    this._syncPanel(panel);
  }

  _panelHtml() {
    const llm = this.llmEnabled ? `已启用：${escapeHtml(this.llmModel || '?')}` : '未配置';
    return `
      <div class="i18n-head"><span>翻译设置</span><button class="i18n-close" title="关闭">×</button></div>
      <div class="i18n-row"><label>主语言</label><div>
        <label><input type="radio" name="i18n-lang" value="zh_Hans"> 简体</label>
        <label><input type="radio" name="i18n-lang" value="zh_Hant"> 繁体</label>
      </div></div>
      <div class="i18n-row"><label>显示</label><div>
        <label><input type="radio" name="i18n-layout" value="zh"> 仅中文</label>
        <label><input type="radio" name="i18n-layout" value="bilingual"> 中日双语</label>
        <label><input type="radio" name="i18n-layout" value="ja"> 仅日文</label>
      </div></div>
      <div class="i18n-row"><label>角色名翻译</label><input type="checkbox" id="i18n-names"></div>
      <div class="i18n-row"><label>AI 译文标记 ⚡</label><input type="checkbox" id="i18n-aimark"></div>
      <div class="i18n-row"><label>玩家名（&lt;user&gt;）</label><input type="text" id="i18n-playername" placeholder="留空＝司令官" maxlength="24"></div>
      <div class="i18n-row llm"><label>AI 补全</label><span class="i18n-llm-status">${llm}</span></div>
      <details class="i18n-llm-config">
        <summary>配置 AI 补全（选填，未译句用你的大模型 API 翻译）</summary>
        <div class="i18n-row"><label>API Key</label><input type="password" id="i18n-llm-key" placeholder="sk-... / 留空＝不改动"></div>
        <div class="i18n-row"><label>模型</label><input type="text" id="i18n-llm-model" placeholder="deepseek-v4-flash"></div>
        <div class="i18n-row"><label>Base URL</label><input type="text" id="i18n-llm-baseurl" placeholder="https://api.deepseek.com"></div>
        <div class="i18n-actions"><button id="i18n-llm-save">保存并启用</button><span class="i18n-llm-save-note"></span></div>
      </details>
      <div class="i18n-actions">
        <button id="i18n-export">导出配置</button>
        <button id="i18n-import">导入配置</button>
        <input type="file" id="i18n-import-file" accept="application/json" hidden>
      </div>`;
  }

  _syncPanel(panel) {
    const s = this.settings;
    const langEl = panel.querySelector(`input[name="i18n-lang"][value="${s.lang}"]`);
    if (langEl) langEl.checked = true;
    const layoutEl = panel.querySelector(`input[name="i18n-layout"][value="${s.layout}"]`);
    if (layoutEl) layoutEl.checked = true;
    panel.querySelector('#i18n-names').checked = !!s.translateNames;
    panel.querySelector('#i18n-aimark').checked = !!s.aiMark;
    panel.querySelector('#i18n-playername').value = s.playerName || '';
  }

  _exportConfig() {
    const blob = new Blob([JSON.stringify({ settings: this.settings }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'config.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export const translator = new Translator();
