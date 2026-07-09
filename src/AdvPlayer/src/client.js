/*
 * client.js — desktop client shell UI, decoupled from the player core.
 *
 * Talks only to the backend orchestration API (/api/state|setup|progress|update|repair)
 * and to the player's public `window.manualReader` hook ({app, loadStory, replayTo}).
 * If neither is present (e.g. plain static hosting), it silently no-ops so the player
 * keeps working standalone.
 *
 *   - First-run wizard: disk/long-path check + one-click download with live progress.
 *   - Update banner: startup check, manual apply.
 *   - QoL: recent list, favorites, and resume-last-position — all in localStorage.
 */
(() => {
  'use strict';

  const LS = {
    recent: 'dotabyss.client.recent',
    favorites: 'dotabyss.client.favorites',
    progress: 'dotabyss.client.progress',
  };
  const RECENT_MAX = 24;

  const lsGet = (key, fallback) => {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  };
  const lsSet = (key, val) => {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* quota */ }
  };

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }
  const apiPost = (path, body) =>
    api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  const fmtBytes = (n) => {
    if (!n) return '0 MB';
    const mb = n / 1048576;
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(0)} MB`;
  };

  // ---- boot ---------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => { init().catch(() => {}); });

  async function init() {
    let state;
    try {
      state = await api('/api/state');
    } catch {
      return; // no backend → standalone static player, nothing to do
    }
    if (!state.installed) {
      showWizard(state);
    } else {
      mountQoL();
      if (state.sharedAssetsOk === false) showRepairBanner(state);
      else checkUpdate(state).catch(() => {});
    }
  }

  // ---- repair shared assets (audio/emotion) banner ------------------------
  function showRepairBanner(state) {
    const what = (state.missingShared || []).join('、') || '共享资源';
    const banner = el('div', 'client-banner');
    banner.innerHTML = `<span>共享资源缺失（${escapeHtml(what)}），BGM/音效/表情可能无法显示</span>
      <button class="client-btn small primary" data-act="fix">一键修复</button>
      <button class="client-btn small" data-act="dismiss">稍后</button>`;
    document.body.appendChild(banner);
    banner.querySelector('[data-act="dismiss"]').addEventListener('click', () => banner.remove());
    banner.querySelector('[data-act="fix"]').addEventListener('click', async () => {
      banner.innerHTML = `<span>修复中（仅重下公共资源，不动剧情）…</span><div class="client-bar inline"><span></span></div><span class="client-banner-text"></span>`;
      try { await apiPost('/api/rebuild-base', {}); } catch (e) { banner.querySelector('.client-banner-text').textContent = e.message; return; }
      pollProgress(banner.querySelector('.client-bar span'), banner.querySelector('.client-banner-text'), () => location.reload());
    });
  }

  // ---- first-run wizard ---------------------------------------------------
  function showWizard(state) {
    const lowDisk = state.diskFree != null && state.diskFree < 6 * 1024 ** 3;
    const overlay = el('div', 'client-overlay');
    overlay.innerHTML = `
      <div class="client-card">
        <h1>ドットアビス 剧情播放器</h1>
        <p class="client-sub">首次使用需要下载并解密剧情资源（约 4 GB，下载+提取需要几十分钟）。</p>
        <ul class="client-facts">
          <li>数据目录：<code>${escapeHtml(state.dataDir)}</code></li>
          <li>可用磁盘：<b class="${lowDisk ? 'warn' : ''}">${state.diskFree != null ? fmtBytes(state.diskFree) : '未知'}</b>（建议 ≥ 6 GB）</li>
          <li>长路径支持：${state.longPathsOk ? '<b class="ok">已启用</b>' : '<b class="warn">未启用（部分语音可能缺失）</b>'}</li>
        </ul>
        ${state.longPathsOk ? '' : `<p class="client-warn">Windows 长路径未启用。建议以管理员身份启用后重开，否则深路径语音会缺失。</p>`}
        <div class="client-progress" hidden>
          <div class="client-prog-row">
            <label>下载</label>
            <div class="client-bar dl"><span></span></div>
            <span class="client-prog-num" data-num="dl"></span>
          </div>
          <div class="client-prog-row">
            <label>提取</label>
            <div class="client-bar ex"><span></span></div>
            <span class="client-prog-num" data-num="ex"></span>
          </div>
          <div class="client-progress-text"></div>
        </div>
        <div class="client-actions">
          <button class="client-btn primary" data-act="start">开始下载</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const startBtn = overlay.querySelector('[data-act="start"]');
    const progBox = overlay.querySelector('.client-progress');
    const dlBar = overlay.querySelector('.client-bar.dl span');
    const exBar = overlay.querySelector('.client-bar.ex span');
    const dlNum = overlay.querySelector('[data-num="dl"]');
    const exNum = overlay.querySelector('[data-num="ex"]');
    const statusEl = overlay.querySelector('.client-progress-text');

    startBtn.addEventListener('click', async () => {
      startBtn.disabled = true;
      startBtn.textContent = '下载中…';
      progBox.hidden = false;
      try {
        await apiPost('/api/setup', {});
      } catch (e) {
        statusEl.textContent = `启动失败：${e.message}`;
        startBtn.disabled = false;
        startBtn.textContent = '重试';
        return;
      }
      _poll(applyDual(dlBar, exBar, dlNum, exNum, statusEl), () => { location.reload(); });
    });
  }

  const PHASE_LABEL = { planning: '准备中', base: '公共资源', stories: '剧情', translations: '翻译', updating: '更新中', done: '完成' };

  // Shared poll loop: apply(progress) each tick; onDone() when finished (not on error).
  function _poll(apply, onDone) {
    const tick = async () => {
      let p;
      try { p = await api('/api/progress'); } catch { setTimeout(tick, 1500); return; }
      apply(p);
      if (p.finished || p.phase === 'done' || p.phase === 'error') {
        if (p.phase !== 'error') onDone();
        return;
      }
      setTimeout(tick, 1500);
    };
    tick();
  }

  // Single bar (used by update / repair banners).
  function pollProgress(bar, text, onDone) {
    _poll((p) => {
      const pct = p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : (p.finished ? 100 : 5);
      if (bar) bar.style.width = `${pct}%`;
      if (!text) return;
      if (p.phase === 'error') { text.textContent = `出错：${p.message || ''}`; return; }
      const phase = PHASE_LABEL[p.phase] || p.phase;
      text.textContent = `${phase} · ${p.done}/${p.total || '?'} · 已下载 ${fmtBytes(p.bytes)}` +
        (p.currentStory ? ` · ${p.currentStory}` : '') +
        (p.errors && p.errors.length ? ` · ${p.errors.length} 处出错` : '');
    }, onDone);
  }

  // Dual bars (wizard): download (leads) + extract (trails), both by story count.
  function applyDual(dlBar, exBar, dlNum, exNum, statusEl) {
    return (p) => {
      const total = p.total || 0;
      const pct = (n) => total ? Math.min(100, Math.round((n / total) * 100)) : (p.finished ? 100 : 0);
      dlBar.style.width = `${total ? pct(p.downloaded) : (p.finished ? 100 : 5)}%`;
      exBar.style.width = `${pct(p.done)}%`;
      dlNum.textContent = total ? `${p.downloaded}/${total} 篇 · ${fmtBytes(p.bytes)}` : fmtBytes(p.bytes);
      exNum.textContent = total ? `${p.done}/${total} 篇` : '';
      if (p.phase === 'error') { statusEl.textContent = `出错：${p.message || ''}`; return; }
      const BASE_LABEL = { 'base:background': '背景', 'base:charastand': '立绘', 'base:se': '音效', 'base:bgm': 'BGM' };
      let s;
      if (p.phase === 'base') s = `公共资源（${BASE_LABEL[p.message] || p.message || '资源'}）提取中…`;
      else if (p.phase === 'translations') s = '翻译下载中…';
      else if (p.phase === 'stories') s = `剧情提取中${p.currentStory ? ' · ' + p.currentStory : ''}`;
      else s = PHASE_LABEL[p.phase] || p.phase;
      if (p.errors && p.errors.length) s += ` · ${p.errors.length} 处出错`;
      statusEl.textContent = s;
    };
  }

  // ---- update banner ------------------------------------------------------
  async function checkUpdate(state) {
    const r = await api('/api/update/check');
    if (!r.resourceUpdate && !r.translationUpdate) return;
    const banner = el('div', 'client-banner');
    const what = [r.resourceUpdate ? '剧情资源' : null, r.translationUpdate ? '翻译' : null].filter(Boolean).join(' 与 ');
    banner.innerHTML = `<span>发现${what}更新${r.newVersion ? `（v${r.newVersion}）` : ''}</span>
      <button class="client-btn small primary" data-act="apply">立即更新</button>
      <button class="client-btn small" data-act="dismiss">稍后</button>`;
    document.body.appendChild(banner);
    banner.querySelector('[data-act="dismiss"]').addEventListener('click', () => banner.remove());
    banner.querySelector('[data-act="apply"]').addEventListener('click', async () => {
      banner.innerHTML = `<span>更新中…</span><div class="client-bar inline"><span></span></div><span class="client-banner-text"></span>`;
      try { await apiPost('/api/update/apply', {}); } catch (e) { banner.querySelector('.client-banner-text').textContent = e.message; return; }
      pollProgress(banner.querySelector('.client-bar span'), banner.querySelector('.client-banner-text'), () => location.reload());
    });
  }

  // ---- QoL: recent / favorites / resume -----------------------------------
  function reader() { return window.manualReader || null; }
  function storyTitle(id) {
    const idx = reader()?.app?.index?.stories;
    const meta = idx && idx.find((s) => s.id === id);
    return meta ? (meta.scriptTitle || meta.title || id) : id;
  }

  function mountQoL() {
    const panel = document.querySelector('.story-filter') || document.querySelector('#storyList')?.parentElement;
    if (!panel) { setTimeout(mountQoL, 500); return; }
    const bar = el('div', 'client-qol');
    bar.innerHTML = `
      <div class="client-qol-row">
        <button class="client-chip" data-tab="resume" title="继续上次">▶ 继续</button>
        <button class="client-chip" data-tab="recent">🕘 最近</button>
        <button class="client-chip" data-tab="fav">⭐ 收藏</button>
        <button class="client-chip" data-act="favtoggle" title="收藏/取消当前剧情">☆</button>
      </div>
      <div class="client-qol-list" hidden></div>`;
    panel.parentElement ? panel.parentElement.insertBefore(bar, panel) : panel.prepend(bar);

    const list = bar.querySelector('.client-qol-list');
    const render = (tab) => {
      list.hidden = false;
      const favs = lsGet(LS.favorites, []);
      const recents = lsGet(LS.recent, []);
      const progress = lsGet(LS.progress, {});
      let ids = [];
      if (tab === 'fav') ids = favs;
      else if (tab === 'recent') ids = recents;
      else if (tab === 'resume') ids = recents.filter((id) => (progress[id] || 0) > 0).slice(0, 8);
      if (!ids.length) { list.innerHTML = `<div class="client-empty">（空）</div>`; return; }
      list.innerHTML = ids.map((id) => {
        const at = progress[id] || 0;
        return `<button class="client-qol-item" data-id="${escapeHtml(id)}">
          <span class="client-qol-title">${escapeHtml(storyTitle(id))}</span>
          ${at > 0 ? `<span class="client-qol-at">第 ${at + 1} 句</span>` : ''}
          ${favs.includes(id) ? '<span class="client-qol-star">⭐</span>' : ''}
        </button>`;
      }).join('');
      list.querySelectorAll('.client-qol-item').forEach((btn) => {
        btn.addEventListener('click', () => openStory(btn.dataset.id, tab === 'resume' || tab === 'recent'));
      });
    };

    bar.querySelectorAll('[data-tab]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const active = chip.classList.contains('active');
        bar.querySelectorAll('[data-tab]').forEach((c) => c.classList.remove('active'));
        if (active) { list.hidden = true; return; }
        chip.classList.add('active');
        render(chip.dataset.tab);
      });
    });
    bar.querySelector('[data-act="favtoggle"]').addEventListener('click', () => {
      const cur = reader()?.app?.story?.id || reader()?.app?.storyMeta?.id;
      if (!cur) return;
      const favs = lsGet(LS.favorites, []);
      const i = favs.indexOf(cur);
      if (i >= 0) favs.splice(i, 1); else favs.unshift(cur);
      lsSet(LS.favorites, favs);
      updateFavToggle(bar);
    });

    updateFavToggle(bar);
    startTracker(bar);
  }

  function updateFavToggle(bar) {
    const btn = bar.querySelector('[data-act="favtoggle"]');
    const cur = reader()?.app?.story?.id || reader()?.app?.storyMeta?.id;
    const favs = lsGet(LS.favorites, []);
    btn.textContent = cur && favs.includes(cur) ? '⭐' : '☆';
  }

  async function openStory(id, resume) {
    const r = reader();
    if (!r) return;
    await r.loadStory(id);
    if (resume) {
      const at = (lsGet(LS.progress, {})[id]) || 0;
      if (at > 1 && typeof r.replayTo === 'function') {
        setTimeout(() => { try { r.replayTo(at); } catch { /* ignore */ } }, 60);
      }
    }
  }

  // Poll the player state to record recent + resume position (no monkeypatching).
  let lastStory = null;
  function startTracker(bar) {
    const save = () => {
      const app = reader()?.app;
      const id = app?.story?.id;
      if (!id) return;
      if (id !== lastStory) {
        lastStory = id;
        const recents = lsGet(LS.recent, []).filter((x) => x !== id);
        recents.unshift(id);
        lsSet(LS.recent, recents.slice(0, RECENT_MAX));
        updateFavToggle(bar);
      }
      if (typeof app.current === 'number' && app.current > 0) {
        const progress = lsGet(LS.progress, {});
        progress[id] = app.current;
        lsSet(LS.progress, progress);
      }
    };
    setInterval(save, 3000);
    window.addEventListener('beforeunload', save);
  }

  // ---- tiny DOM helpers ---------------------------------------------------
  function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
})();
