// ==UserScript==
// @name         Pi Web: Cmd+Enter to Send
// @namespace    pi.web.shortcuts
// @version      1.0.0
// @description  Pi Web 聊天输入框改为编辑器风格：裸 Enter 插入换行，Cmd/Ctrl+Enter 发送。
// @author       you
// @match        http://127.0.0.1:30141/*
// @match        http://localhost:30141/*
// @match        http://127.0.0.1:31415/*
// @match        http://localhost:31415/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/**
 * 原理：
 *   pi-web 默认「裸 Enter 发送、Shift+Enter 换行」。
 *   本脚本在捕获阶段拦截聊天输入框的 keydown：
 *     - 裸 Enter（无 Cmd/Ctrl） → 手动插入换行，阻止 pi-web 发送
 *     - Cmd/Ctrl+Enter           → 放行，交给 pi-web 发送
 *     - 命令/文件/技能补全弹窗打开时 → 放行裸 Enter，让补全项可被选中（类似 VS Code）
 *     - 中文输入法组合输入中 → 放行（Enter 用于确认候选词）
 */

(function () {
  "use strict";

  /** 聊天输入框特征：textarea + 内联样式 flex:1 + width:100% + resize:none */
  function isChatInput(el) {
    if (!el || el.tagName !== "TEXTAREA") return false;
    const s = el.style;
    if (s.flex !== "1" || s.width !== "100%" || s.resize !== "none") return false;
    return true;
  }

  /** 检测输入框上方的补全弹窗是否可见（命令 / 文件 / 技能，zIndex:120） */
  function suggestionVisible(el) {
    let p = el.parentElement;
    while (p) {
      if (p.style && p.style.position === "relative") {
        return Array.from(p.children).some((c) => {
          if (c === el) return false;
          const cs = getComputedStyle(c);
          return (
            cs.position === "absolute" &&
            cs.zIndex === "120" &&
            c.offsetParent !== null &&
            c.childElementCount > 0
          );
        });
      }
      p = p.parentElement;
    }
    return false;
  }

  /** 在光标处插入换行并触发 React onChange */
  function insertNewline(el) {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    ).set;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    const v = el.value;
    setter.call(el, v.slice(0, start) + "\n" + v.slice(end));
    const pos = start + 1;
    el.setSelectionRange(pos, pos);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  window.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Enter") return;

      // Cmd/Ctrl+Enter → 发送（放行给 pi-web）
      if (e.metaKey || e.ctrlKey) return;

      // 中文输入法组合输入中 → 放行（Enter 确认候选词）
      if (e.isComposing || e.keyCode === 229) return;

      const el = e.target;
      if (!isChatInput(el)) return;

      // 补全弹窗打开 → 放行（Enter 用于选中补全项）
      if (suggestionVisible(el)) return;

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      insertNewline(el);
    },
    true,
  );
})();
