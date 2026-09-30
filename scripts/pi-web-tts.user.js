// ==UserScript==
// @name         Pi Web: Click to Read Replies (TTS)
// @namespace    pi.web.tts
// @version      1.6.0
// @description  每条 AI 回复带「朗读」按钮：优先走本地 edge-tts 神经语音服务（真人音质，50+ 音色可选、支持搜索），服务不可用自动回退系统语音；带进度条与停止按钮，自动跳过代码块。
// @author       you
// @match        http://127.0.0.1:30141/*
// @match        http://localhost:30141/*
// @match        http://127.0.0.1:31415/*
// @match        http://localhost:31415/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/**
 * 原理：
 *   pi-web 的消息正文渲染在 div.markdown-body 内（用户消息额外带 markdown-user-message，
 *   可据此只朗读 AI 回复）；代码块是 div.markdown-code-block、行内代码是 code.markdown-inline-code，
 *   提取文本时全部跳过。脚本自动给每条 AI 回复正文上方注入「朗读」按钮。
 *
 * 两种引擎：
 *   1. 神经语音服务（默认）：本地 tts-server（edge-tts，http://127.0.0.1:8971）现生成 mp3 播放，
 *      支持几十种真人音色，控制条可点「声音」切换，选择存 localStorage。
 *   2. 系统语音（回退）：服务未启动/断网时自动用 Web Speech API。
 *
 * 交互：
 *   - 朗读按钮是主要入口，无需选中/拖拽文字
 *   - 点击正文本身也可触发朗读（备用入口）；点击链接/代码区域、拖选文字均不触发
 *   - 朗读时底部出现控制条：声音切换 + 进度条 + 停止
 */

(function () {
  "use strict";

  // ========== 配置 ==========
  const TTS_SERVER = "http://127.0.0.1:8971"; // 本地 edge-tts 服务地址
  const RATE = 1.0; // 语速：0.8 慢 / 1.2 快
  const CHUNK_MAX = 4000; // 单次合成最大字符数，超长文本自动按句切块
  const MAX_LENGTH = 50000; // 单条消息最多朗读字符数
  const PENDING_MS = 140; // 点击后延迟朗读，用于区分双击/拖选

  // 内置声音列表（服务 /voices 会覆盖它）
  let VOICES = [
    { id: "zh-CN-XiaoxiaoNeural", name: "晓晓（女·温柔）" },
    { id: "zh-CN-XiaoyiNeural", name: "晓伊（女·活泼）" },
    { id: "zh-CN-YunxiNeural", name: "云希（男·阳光）" },
    { id: "zh-CN-YunyangNeural", name: "云扬（男·新闻）" },
    { id: "zh-CN-YunxiaNeural", name: "云夏（童声）" },
    { id: "zh-CN-liaoning-XiaobeiNeural", name: "晓北（东北话）" },
    { id: "zh-CN-shaanxi-XiaoniNeural", name: "晓妮（陕西话）" },
    { id: "zh-TW-HsiaoChenNeural", name: "曉臻（台湾）" },
    { id: "zh-HK-HiuMaanNeural", name: "曉曼（粤语）" },
    { id: "en-US-AriaNeural", name: "Aria（英文·女）" },
    { id: "en-US-GuyNeural", name: "Guy（英文·男）" },
    { id: "en-US-JennyNeural", name: "Jenny（英文·女）" },
  ];
  let currentVoice =
    localStorage.getItem("pi-tts-voice") || VOICES[0].id;
  let serverOk = false;

  // ========== 状态 ==========
  let currentEl = null;
  let speaking = false;
  let chunkCancelled = false;
  let keepHighlightTimer = null;
  let pendingSpeak = null; // { body, timer }
  let audioEl = null; // 神经语音模式当前播放的 <audio>

  // ========== 工具 ==========
  /** 是否为 AI 回复正文（排除用户消息 / 摘要 / 自定义消息） */
  function isAssistantBody(el) {
    return (
      el.classList &&
      el.classList.contains("markdown-body") &&
      !el.classList.contains("markdown-user-message") &&
      !el.classList.contains("markdown-compaction-message") &&
      !el.classList.contains("markdown-custom-message")
    );
  }

  /** 收集可朗读文本：跳过代码块、行内代码、pre/code 与隐藏内容 */
  function collectText(root) {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const cs = getComputedStyle(parent);
        if (cs.display === "none" || cs.visibility === "hidden") return NodeFilter.FILTER_REJECT;
        if (
          parent.closest(".markdown-code-block") ||
          parent.closest(".markdown-inline-code") ||
          parent.tagName === "PRE" ||
          parent.tagName === "CODE"
        ) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) {
      const t = walker.currentNode.textContent.replace(/\s+/g, " ").trim();
      if (t) parts.push(t);
    }
    return parts.join(" ").trim();
  }

  /** 按内容中文字符占比自动选择语言（系统语音回退时用） */
  function detectLang(text) {
    const zh = (text.match(/[\u4e00-\u9fff]/g) || []).length;
    return zh > 0 && zh / text.length >= 0.12 ? "zh" : "en";
  }

  /** 系统语音：质量优先挑选 */
  function pickVoice(lang) {
    const voices = speechSynthesis.getVoices();
    if (!voices.length) return null;
    const wanted =
      lang === "zh"
        ? ["zh-cn", "zh-tw", "zh-hk", "zh"]
        : ["en-us", "en-gb", "en-au", "en"];
    const candidates = voices.filter(
      (v) => v.lang && wanted.includes(v.lang.toLowerCase()),
    );
    if (!candidates.length) return null;
    const quality = [
      "enhanced",
      "natural",
      "premium",
      "neural",
      "xiaoxiao",
      "yunxi",
      "yunyang",
      "tingting",
      "siri",
    ];
    for (const q of quality) {
      const v = candidates.find((c) => c.name.toLowerCase().includes(q));
      if (v) return v;
    }
    return candidates[0];
  }

  /** 超长文本按句子/空格切块 */
  function splitChunks(text) {
    const out = [];
    let rest = text;
    while (rest.length > CHUNK_MAX) {
      const seps = ["。", "！", "？", "\n", "，", " "];
      let cut = -1;
      for (const sep of seps) {
        cut = rest.lastIndexOf(sep, CHUNK_MAX);
        if (cut >= CHUNK_MAX * 0.4) break;
        cut = -1;
      }
      if (cut < CHUNK_MAX * 0.4) cut = CHUNK_MAX;
      out.push(rest.slice(0, cut + 1));
      rest = rest.slice(cut + 1);
    }
    if (rest) out.push(rest);
    return out;
  }

  /** 声音短名（按钮/菜单显示用） */
  function voiceShortName(id) {
    const v = VOICES.find((x) => x.id === id);
    if (v) return v.name.split("（")[0];
    const m = id.match(/-(Neural)$/);
    return m ? id.split("-").slice(0, -1).pop() : id;
  }

  // ========== Toast 提示 ==========
  let toastEl = null;
  function toast(msg, ms = 1800) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.id = "pi-tts-toast";
      Object.assign(toastEl.style, {
        position: "fixed",
        left: "50%",
        bottom: "28px",
        transform: "translateX(-50%)",
        background: "rgba(20,20,25,.82)",
        color: "#fff",
        padding: "9px 16px",
        borderRadius: "999px",
        fontSize: "13px",
        lineHeight: 1.4,
        zIndex: "2147483647",
        display: "none",
        alignItems: "center",
        gap: "8px",
        pointerEvents: "none",
        boxShadow: "0 4px 18px rgba(0,0,0,.25)",
        maxWidth: "80vw",
      });
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.style.display = "flex";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      toastEl.style.display = "none";
    }, ms);
  }

  // ========== 播放控制条（声音 + 进度 + 停止） ==========
  let ctrlBar = null;
  let ctrlFill = null;
  let voiceBtnEl = null;
  let voiceMenu = null;

  function buildCtrlBar() {
    if (ctrlBar) return;
    ctrlBar = document.createElement("div");
    ctrlBar.id = "pi-tts-ctrl";
    Object.assign(ctrlBar.style, {
      position: "fixed",
      left: "50%",
      bottom: "70px",
      transform: "translateX(-50%)",
      display: "none",
      alignItems: "center",
      gap: "8px",
      background: "rgba(20,20,25,.82)",
      color: "#fff",
      padding: "8px 12px",
      borderRadius: "12px",
      fontSize: "12px",
      zIndex: "2147483646",
      boxShadow: "0 4px 18px rgba(0,0,0,.25)",
      width: "min(480px, 74vw)",
    });

    // 声音切换按钮
    voiceBtnEl = document.createElement("button");
    voiceBtnEl.type = "button";
    voiceBtnEl.title = "切换声音";
    Object.assign(voiceBtnEl.style, {
      display: "inline-flex",
      alignItems: "center",
      gap: "3px",
      padding: "3px 8px",
      border: "1px solid rgba(255,255,255,.25)",
      borderRadius: "7px",
      background: "rgba(255,255,255,.08)",
      color: "#fff",
      fontSize: "11px",
      cursor: "pointer",
      whiteSpace: "nowrap",
      flexShrink: "0",
    });
    voiceBtnEl.textContent = "声音 " + voiceShortName(currentVoice);
    voiceBtnEl.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleVoiceMenu();
    });

    // 进度条
    const track = document.createElement("div");
    Object.assign(track.style, {
      flex: "1",
      height: "4px",
      background: "rgba(255,255,255,.18)",
      borderRadius: "2px",
      overflow: "hidden",
    });
    ctrlFill = document.createElement("div");
    Object.assign(ctrlFill.style, {
      height: "100%",
      width: "0%",
      background: "#3b82f6",
      borderRadius: "2px",
      transition: "width .25s linear",
    });
    track.appendChild(ctrlFill);

    // 停止按钮
    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    Object.assign(stopBtn.style, {
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      width: "26px",
      height: "26px",
      border: "none",
      borderRadius: "7px",
      background: "#ef4444",
      color: "#fff",
      cursor: "pointer",
      flexShrink: "0",
    });
    stopBtn.innerHTML =
      '<svg viewBox="0 0 16 16" fill="currentColor" style="width:13px;height:13px"><rect x="3.5" y="3.5" width="9" height="9" rx="1.5"/></svg>';
    stopBtn.title = "停止朗读";
    stopBtn.addEventListener("click", () => stop());

    ctrlBar.append(voiceBtnEl, track, stopBtn);
    document.body.appendChild(ctrlBar);
  }

  function showCtrl() {
    buildCtrlBar();
    voiceBtnEl.textContent = "声音 " + voiceShortName(currentVoice);
    ctrlBar.style.display = "flex";
  }

  function hideCtrl() {
    closeVoiceMenu();
    if (ctrlBar) ctrlBar.style.display = "none";
  }

  function setProgress(ratio) {
    if (ctrlFill) {
      ctrlFill.style.width =
        Math.min(100, Math.max(0, ratio * 100)).toFixed(1) + "%";
    }
  }

  // ========== 声音选择菜单（搜索 + 分组） ==========
  let voiceInput = null;
  let voiceListEl = null;

  function groupOf(id) {
    if (id.startsWith("zh-CN")) return "中文（普通话）";
    if (id.startsWith("zh-")) return "中文（方言 / 台 / 港）";
    if (id.startsWith("en-")) return "英文";
    if (id.startsWith("ja-")) return "日语";
    if (id.startsWith("ko-")) return "韩语";
    if (id.startsWith("fr-")) return "法语";
    if (id.startsWith("de-")) return "德语";
    if (id.startsWith("es-")) return "西班牙语";
    if (id.startsWith("ru-")) return "俄语";
    if (id.startsWith("pt-")) return "葡萄牙语";
    if (id.startsWith("it-")) return "意大利语";
    return "其它";
  }

  function toggleVoiceMenu() {
    if (voiceMenu && voiceMenu.style.display !== "none") {
      closeVoiceMenu();
      return;
    }
    if (!serverOk) {
      toast(
        "⚠ 神经语音服务未连接，当前是系统声音。请双击桌面「启动-Pi朗读服务」，然后刷新页面。",
        3500,
      );
    }
    buildVoiceMenu();
    if (voiceInput) voiceInput.value = "";
    renderVoiceList("");
    voiceMenu.style.display = "block";
    const rect = voiceBtnEl.getBoundingClientRect();
    voiceMenu.style.left = rect.left + "px";
    voiceMenu.style.top = rect.top - voiceMenu.offsetHeight - 6 + "px";
    if (voiceInput) voiceInput.focus();
  }

  function buildVoiceMenu() {
    if (voiceMenu) return;
    voiceMenu = document.createElement("div");
    voiceMenu.id = "pi-tts-voice-menu";
    Object.assign(voiceMenu.style, {
      position: "fixed",
      display: "none",
      background: "rgba(28,28,34,.96)",
      color: "#fff",
      borderRadius: "10px",
      padding: "6px",
      fontSize: "12px",
      zIndex: "2147483647",
      boxShadow: "0 8px 30px rgba(0,0,0,.4)",
      minWidth: "200px",
    });
    voiceMenu.addEventListener("click", (e) => e.stopPropagation());

    voiceInput = document.createElement("input");
    voiceInput.type = "text";
    voiceInput.placeholder = "搜索音色…";
    Object.assign(voiceInput.style, {
      width: "100%",
      boxSizing: "border-box",
      padding: "6px 10px",
      border: "1px solid rgba(255,255,255,.2)",
      borderRadius: "6px",
      background: "rgba(255,255,255,.08)",
      color: "#fff",
      fontSize: "12px",
      outline: "none",
    });
    voiceInput.addEventListener("input", () => {
      renderVoiceList(voiceInput.value.trim().toLowerCase());
    });
    voiceInput.addEventListener("keydown", (e) => e.stopPropagation());

    voiceListEl = document.createElement("div");
    Object.assign(voiceListEl.style, {
      marginTop: "4px",
      maxHeight: "320px",
      overflowY: "auto",
    });

    voiceMenu.append(voiceInput, voiceListEl);
    document.body.appendChild(voiceMenu);
  }

  function renderVoiceList(filter) {
    buildVoiceMenu();
    voiceListEl.textContent = "";
    let lastGroup = null;
    VOICES.forEach((v) => {
      if (filter && !(v.id + " " + v.name).toLowerCase().includes(filter)) return;
      const g = groupOf(v.id);
      if (g !== lastGroup) {
        lastGroup = g;
        const h = document.createElement("div");
        h.textContent = g;
        Object.assign(h.style, {
          padding: "6px 10px 3px",
          fontSize: "11px",
          color: "#9ca3af",
        });
        voiceListEl.appendChild(h);
      }
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = v.name;
      item.title = v.id;
      Object.assign(item.style, {
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "6px 10px",
        border: "none",
        borderRadius: "6px",
        background: v.id === currentVoice ? "rgba(59,130,246,.35)" : "transparent",
        color: "#fff",
        fontSize: "12px",
        cursor: "pointer",
      });
      item.addEventListener("mouseenter", () => {
        item.style.background =
          v.id === currentVoice ? "rgba(59,130,246,.45)" : "rgba(255,255,255,.1)";
      });
      item.addEventListener("mouseleave", () => {
        item.style.background =
          v.id === currentVoice ? "rgba(59,130,246,.35)" : "transparent";
      });
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        currentVoice = v.id;
        localStorage.setItem("pi-tts-voice", currentVoice);
        voiceBtnEl.textContent = "声音 " + voiceShortName(currentVoice);
        closeVoiceMenu();
        // 正在朗读时切换 → 立即用新音色重播当前回复
        if (speaking && currentEl) {
          toast("已切换为 " + v.name + "，重新朗读中…", 2200);
          speak(currentEl, { restart: true });
        } else {
          toast("已切换为 " + v.name);
        }
      });
      voiceListEl.appendChild(item);
    });
    if (!voiceListEl.childElementCount) {
      const empty = document.createElement("div");
      empty.textContent = "没有匹配的音色";
      Object.assign(empty.style, { padding: "8px 10px", color: "#9ca3af" });
      voiceListEl.appendChild(empty);
    }
  }

  function closeVoiceMenu() {
    if (voiceMenu) voiceMenu.style.display = "none";
  }

  // ========== 朗读按钮 ==========
  function injectButtonStyles() {
    const id = "pi-tts-style";
    if (document.getElementById(id)) return;
    const style = document.createElement("style");
    style.id = id;
    style.textContent = `
      .pi-tts-btn-wrap { display:flex; justify-content:flex-end; margin:0 0 2px; }
      .pi-tts-btn {
        display:inline-flex; align-items:center; gap:4px;
        font-size:11px; line-height:1; padding:3px 9px;
        border:1px solid rgba(59,130,246,.35); border-radius:999px;
        background:rgba(59,130,246,.07); color:#8b949e;
        cursor:pointer; user-select:none; opacity:.5;
        transition:opacity .15s, color .15s;
      }
      .pi-tts-btn:hover { opacity:1; color:#3b82f6; }
      .pi-tts-btn.pi-tts-active { opacity:1; color:#fff; background:#3b82f6; border-color:#3b82f6; }
    `;
    document.head.appendChild(style);
  }

  /** 同步单个按钮的朗读状态 */
  function syncButtonFor(body) {
    const wrap = body.previousElementSibling;
    if (!wrap || !(wrap.classList && wrap.classList.contains("pi-tts-btn-wrap"))) return;
    const btn = wrap.firstElementChild;
    if (!btn) return;
    const active = currentEl === body && speaking;
    btn.classList.toggle("pi-tts-active", active);
    btn.title = active ? "停止朗读" : "朗读整段回复（跳过代码）";
    btn.innerHTML = active
      ? '<svg viewBox="0 0 16 16" fill="currentColor" style="width:11px;height:11px"><rect x="3.5" y="3.5" width="9" height="9" rx="1.5"/></svg>停止'
      : '<svg viewBox="0 0 16 16" fill="currentColor" style="width:11px;height:11px"><path d="M3.2 2.4v11.2c0 .7.8 1.1 1.4.7l8.7-5.6c.5-.4.5-1.1 0-1.4L4.6 1.7c-.6-.4-1.4 0-1.4.7z"/></svg>朗读';
  }

  function updateAllButtons() {
    document.querySelectorAll(".pi-tts-btn-wrap").forEach((wrap) => {
      const body = wrap.nextElementSibling;
      if (body && isAssistantBody(body)) syncButtonFor(body);
    });
  }

  /** 为所有 AI 回复正文补上朗读按钮（React 重渲染可能清掉，定时重扫） */
  function ensureButtons() {
    document.querySelectorAll(".markdown-body").forEach((body) => {
      if (!isAssistantBody(body) || !body.parentElement) return;
      const prev = body.previousElementSibling;
      if (prev && prev.classList && prev.classList.contains("pi-tts-btn-wrap")) {
        syncButtonFor(body);
        return;
      }
      const wrap = document.createElement("div");
      wrap.className = "pi-tts-btn-wrap";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pi-tts-btn";
      btn.innerHTML =
        '<svg viewBox="0 0 16 16" fill="currentColor" style="width:11px;height:11px"><path d="M3.2 2.4v11.2c0 .7.8 1.1 1.4.7l8.7-5.6c.5-.4.5-1.1 0-1.4L4.6 1.7c-.6-.4-1.4 0-1.4.7z"/></svg>朗读';
      wrap.appendChild(btn);
      body.parentElement.insertBefore(wrap, body);
      syncButtonFor(body);
    });
  }

  // 监听 DOM 变化，持续维护按钮（流式输出/新消息）
  let scanTimer = null;
  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => ensureButtons(), 300);
  }
  function startObserver() {
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === "childList" && (m.addedNodes.length || m.removedNodes.length)) {
          scheduleScan();
          return;
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ========== 高亮正在朗读的消息 ==========
  function highlight(el) {
    if (!el) return;
    el.style.outline = "2px solid #3b82f6";
    el.style.outlineOffset = "2px";
    el.style.borderRadius = "4px";
    clearInterval(keepHighlightTimer);
    keepHighlightTimer = setInterval(() => {
      if (currentEl && currentEl.isConnected) {
        currentEl.style.outline = "2px solid #3b82f6";
        currentEl.style.outlineOffset = "2px";
      }
    }, 400);
  }

  function clearHighlight() {
    clearInterval(keepHighlightTimer);
    keepHighlightTimer = null;
    if (currentEl) {
      currentEl.style.outline = "";
      currentEl.style.outlineOffset = "";
    }
  }

  // ========== 朗读控制 ==========
  function stop({ silent = false } = {}) {
    const wasSpeaking = speaking;
    chunkCancelled = true;
    speechSynthesis.cancel();
    if (audioEl) {
      audioEl.pause();
      audioEl.onended = null;
      audioEl.ontimeupdate = null;
      audioEl = null;
    }
    speaking = false;
    clearHighlight();
    updateAllButtons();
    hideCtrl();
    if (wasSpeaking && !silent) toast("已停止朗读");
  }

  async function speak(el, { restart = false } = {}) {
    const text = collectText(el);
    if (!text) {
      toast("这条回复没有可朗读的文字");
      return;
    }
    if (!restart && currentEl === el && speaking) {
      stop();
      currentEl = null;
      return;
    }

    // 服务可能是在页面打开之后才启动的：点击朗读时重新探测一次
    if (!serverOk) {
      try {
        const r = await fetch(`${TTS_SERVER}/health`, {
          signal: AbortSignal.timeout(1000),
        });
        if (r.ok) {
          serverOk = true;
          renderVoiceList("");
        }
      } catch {}
    }

    chunkCancelled = true;
    speechSynthesis.cancel();
    if (audioEl) {
      audioEl.pause();
      audioEl.onended = null;
      audioEl.ontimeupdate = null;
      audioEl = null;
    }

    const limited =
      text.length > MAX_LENGTH
        ? text.slice(0, MAX_LENGTH) + "（内容过长，已截断）"
        : text;
    const chunks = splitChunks(limited);
    const totalChars = limited.length;

    currentEl = el;
    speaking = true;
    chunkCancelled = false;
    highlight(el);
    updateAllButtons();
    showCtrl();
    setProgress(0);

    const done = () => {
      if (chunkCancelled) return;
      speaking = false;
      currentEl = null;
      clearHighlight();
      updateAllButtons();
      hideCtrl();
      toast("朗读完成");
    };

    if (serverOk) {
      startServerPlay(chunks, totalChars, currentVoice, done);
    } else {
      startWebSpeechPlay(chunks, totalChars, done);
    }
  }

  // ---------- 引擎一：edge-tts 神经语音服务 ----------
  async function fetchAudio(text, voice) {
    const r = await fetch(`${TTS_SERVER}/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice, rate: RATE }),
    });
    if (!r.ok) {
      let msg = String(r.status);
      try {
        const j = await r.json();
        if (j.error) msg = j.error;
      } catch {}
      throw new Error(msg);
    }
    return r.blob();
  }

  function startServerPlay(chunks, totalChars, voice, done) {
    let i = 0;
    const prevLen = (idx) =>
      chunks.slice(0, idx).reduce((s, c) => s + c.length, 0);

    const failCleanup = () => {
      chunkCancelled = true;
      speaking = false;
      currentEl = null;
      clearHighlight();
      updateAllButtons();
      hideCtrl();
    };

    const playNext = async () => {
      if (chunkCancelled) return;
      if (i >= chunks.length) {
        done();
        return;
      }
      const idx = i++;
      const chunk = chunks[idx];
      let blob;
      try {
        blob = await fetchAudio(chunk, voice);
      } catch (err) {
        if (chunkCancelled) return;
        toast("声音服务请求失败：" + err.message);
        failCleanup();
        return;
      }
      if (chunkCancelled) return;

      const url = URL.createObjectURL(blob);
      const a = new Audio(url);
      audioEl = a;
      a.onended = () => {
        URL.revokeObjectURL(url);
        if (chunkCancelled) return;
        setProgress((prevLen(idx) + chunk.length) / totalChars);
        if (i < chunks.length) playNext();
        else done();
      };
      a.ontimeupdate = () => {
        if (!chunkCancelled && a.duration) {
          setProgress(
            (prevLen(idx) + (a.currentTime / a.duration) * chunk.length) /
              totalChars,
          );
        }
      };
      a.onerror = () => {
        URL.revokeObjectURL(url);
        if (chunkCancelled) return;
        toast("音频播放失败");
        failCleanup();
      };
      try {
        await a.play();
      } catch (err) {
        if (chunkCancelled) return;
        toast("自动播放被拦截，请先点一下页面");
        failCleanup();
      }
    };

    playNext();
  }

  // ---------- 引擎二：系统语音（Web Speech API 回退） ----------
  function startWebSpeechPlay(chunks, totalChars, done) {
    const lang = detectLang(chunks.join(" "));
    const voice = pickVoice(lang);
    let i = 0;

    const play = () => {
      if (chunkCancelled) return;
      if (i >= chunks.length) {
        done();
        return;
      }
      const chunk = chunks[i];
      const prevLen = chunks.slice(0, i).reduce((s, c) => s + c.length, 0);
      i++;
      const u = new SpeechSynthesisUtterance(chunk);
      u.lang = voice ? voice.lang : lang;
      if (voice) u.voice = voice;
      u.rate = RATE;
      u.onboundary = (e) => {
        if (chunkCancelled || typeof e.charIndex !== "number") return;
        setProgress((prevLen + e.charIndex) / totalChars);
      };
      u.onend = () => {
        if (chunkCancelled) return;
        setProgress((prevLen + chunk.length) / totalChars);
        if (i < chunks.length) play();
        else done();
      };
      u.onerror = (e) => {
        if (e.error === "interrupted" || e.error === "canceled") return;
        toast("朗读出错：" + e.error);
        speaking = false;
        currentEl = null;
        clearHighlight();
        updateAllButtons();
        hideCtrl();
      };
      speechSynthesis.speak(u);
    };

    setTimeout(play, 60);
  }

  // ========== 神经语音服务探测 ==========
  async function checkServer() {
    try {
      const r = await fetch(`${TTS_SERVER}/health`, {
        signal: AbortSignal.timeout(2000),
      });
      serverOk = r.ok;
      if (serverOk) {
        try {
          const v = await (
            await fetch(`${TTS_SERVER}/voices`, {
              signal: AbortSignal.timeout(2000),
            })
          ).json();
          if (v && Array.isArray(v.voices) && v.voices.length) {
            VOICES = v.voices;
            if (!VOICES.some((x) => x.id === currentVoice)) {
              currentVoice = VOICES[0].id;
              localStorage.setItem("pi-tts-voice", currentVoice);
            }
          }
        } catch {}
        renderVoiceList("");
      }
    } catch {
      serverOk = false;
    }
    if (!serverOk) {
      console.info(
        "[pi-tts] 神经语音服务未启动，回退系统语音。启动方式见 README 或运行: .venv/bin/python tts-server/server.py",
      );
    }
  }

  // ========== 点击交互 ==========
  function clearPending() {
    if (pendingSpeak) {
      clearTimeout(pendingSpeak.timer);
      pendingSpeak = null;
    }
  }

  function startPending(body) {
    clearPending();
    pendingSpeak = {
      body,
      timer: setTimeout(() => {
        pendingSpeak = null;
        speak(body);
      }, PENDING_MS),
    };
  }

  // 双击选词：取消延迟中的朗读，并静默停止可能已开始的朗读
  document.addEventListener(
    "dblclick",
    () => {
      clearPending();
      stop({ silent: true });
    },
    true,
  );

  document.addEventListener(
    "click",
    (e) => {
      clearPending();

      const t = e.target;
      if (!(t instanceof Element)) return;

      // 点击朗读按钮 → 朗读/停止整段回复
      const btn = t.closest(".pi-tts-btn");
      if (btn) {
        e.preventDefault();
        e.stopPropagation();
        const body = btn.closest(".pi-tts-btn-wrap").nextElementSibling;
        if (body && isAssistantBody(body)) speak(body);
        return;
      }

      // 点击声音菜单外部 → 关闭菜单
      if (voiceMenu && voiceMenu.style.display !== "none") {
        if (!voiceMenu.contains(t)) closeVoiceMenu();
      }

      // 可交互元素（链接/按钮/输入框/折叠头等）→ 放行
      if (
        t.closest(
          "a, button, input, textarea, select, [role='button'], [contenteditable='true']",
        )
      ) {
        return;
      }

      // 用户正在拖选文字 → 放行
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim()) return;

      // 代码区域（点击复制等）→ 放行
      if (t.closest("code, pre")) return;

      const body = t.closest(".markdown-body");
      if (!body || !isAssistantBody(body)) return;

      e.preventDefault();
      startPending(body);
    },
    true,
  );

  // ========== 启动 ==========
  injectButtonStyles();
  ensureButtons();
  startObserver();
  renderVoiceList("");
  checkServer();
})();
