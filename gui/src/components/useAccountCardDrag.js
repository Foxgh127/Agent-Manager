import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { containsPoint, gridSlotAt, moveCardToSlot, cardOrderPayload, cardDropTarget, dockCardPreview } from "./dragGeometry.js";
import "./AccountCardDrag.css";

const CARD = "[data-card-id]", TARGET = "[data-card-group-drop],[data-card-pool-drop]";
const INTERACTIVE = "button,a,input,textarea,select,option,label,summary,[contenteditable]:not([contenteditable='false']),[data-no-card-drag],[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=listbox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=slider],[role=spinbutton],[role=textbox],[role=tab],[role=treeitem]";
const GRAPHIC = "svg,img,canvas,video,audio,iframe,object,embed,progress,meter,[role=img]";
const idsOf = items => items.map(item => typeof item === "string" ? item : item.id);
const sameOrder = (a, b) => a.length === b.length && a.every((id, index) => id === b[index]);
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const flipAnimations = node => node.getAnimations?.().filter(animation => animation.id === "account-card-flip") || [];

export function isCardBlankSpace(target, card, point) {
  if (!(target instanceof Element) || !card.contains(target)) return false;
  const interactive = target.closest(INTERACTIVE), focusable = target.closest("[tabindex]"), graphic = target.closest(GRAPHIC);
  if (interactive && card.contains(interactive) || focusable && focusable !== card && card.contains(focusable) || graphic && card.contains(graphic)) return false;
  return !Array.from(target.childNodes).some(node => {
    if (node.nodeType !== 3 || !node.textContent.trim()) return false;
    if (!point || !target.ownerDocument?.createRange) return true;
    const range = target.ownerDocument.createRange(); range.selectNodeContents(node);
    return Array.from(range.getClientRects()).some(rect => point.clientX >= rect.left && point.clientX <= rect.right
      && point.clientY >= rect.top && point.clientY <= rect.bottom);
  });
}

export function keyboardCardMove(ids, sourceId, delta) {
  const index = ids.indexOf(sourceId), target = index + Math.sign(delta);
  if (!delta || index < 0 || target < 0 || target >= ids.length) return null;
  return { sourceId, visibleIds: ids, ...(delta < 0 ? { beforeId: ids[target] } : { afterId: ids[target] }) };
}

// Animated neighbors are not collision targets: remove FLIP's translation to
// obtain their actual grid slots and avoid oscillating as those neighbors move.
function layoutRect(node) {
  const rect = node.getBoundingClientRect(), transform = getComputedStyle(node).transform;
  let x = 0, y = 0;
  if (transform && transform !== "none" && typeof window.DOMMatrixReadOnly === "function") {
    const matrix = new window.DOMMatrixReadOnly(transform); x = matrix.m41; y = matrix.m42;
  }
  return { left: rect.left - x, right: rect.right - x, top: rect.top - y, bottom: rect.bottom - y, width: rect.width, height: rect.height };
}

/** Pass persisted/base items; render previewIds declaratively. onMove resolves
 * after persisted state is installed. Primary mouse + 4px starts, without delay.
 */
export default function useAccountCardDrag({ gridRef, items, groupId = "all", busy = false, onMove, onError }) {
  const latest = useRef({ items, groupId, busy, onMove, onError }); latest.current = { items, groupId, busy, onMove, onError };
  const session = useRef(null), mounted = useRef(true), committing = useRef(false), cancelDrag = useRef(null), previousRects = useRef(new Map());
  const [draggingId, setDraggingId] = useState(null), [saving, setSaving] = useState(false), [previewIds, setPreviewIds] = useState(null);
  const [dropTarget, setDropTarget] = useState(null), [status, setStatus] = useState("");
  const itemKey = JSON.stringify(idsOf(items)), displayKey = JSON.stringify(previewIds || idsOf(items));
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const commit = async payload => {
    if (committing.current || latest.current.busy || !payload) return false;
    committing.current = true;
    if (mounted.current) { setSaving(true); setStatus("正在保存…"); }
    try {
      await latest.current.onMove(payload);
      if (mounted.current) setStatus(payload.dropTarget === "apiPool" ? "已加入本地 API 号池。" : payload.groupId ? "卡片已移入分组。" : "卡片位置已保存。");
      return true;
    } catch (error) {
      if (mounted.current) setStatus("移动未保存，已恢复原位置。");
      try { latest.current.onError?.(error); } catch { /* Notification failure must not strand the drag. */ }
      return false;
    } finally { committing.current = false; if (mounted.current) setSaving(false); }
  };
  const commitRef = useRef(commit); commitRef.current = commit;

  useLayoutEffect(() => {
    const nodes = Array.from(gridRef.current?.querySelectorAll(CARD) || []);
    const next = new Map(nodes.map(node => [node.dataset.cardId, layoutRect(node)]));
    for (const node of nodes) {
      const before = previousRects.current.get(node.dataset.cardId), after = next.get(node.dataset.cardId);
      flipAnimations(node).forEach(animation => animation.cancel());
      if (!reducedMotion() && before && after && Math.abs(before.width - after.width) < 2) {
        const x = before.left - after.left, y = before.top - after.top;
        if (Math.abs(x) + Math.abs(y) > 1) {
          const animation = node.animate?.([{ transform: `translate(${x}px, ${y}px)` }, { transform: "translate(0, 0)" }], { duration: 250, easing: "cubic-bezier(.2,.82,.24,1)" });
          if (animation) animation.id = "account-card-flip";
        }
      }
    }
    previousRects.current = next;
  }, [gridRef, displayKey]);

  useEffect(() => {
    let raf = 0, clickTimer = 0, suppressClick = false;
    const swallowClick = event => { if (suppressClick) { event.preventDefault(); event.stopImmediatePropagation(); suppressClick = false; } };
    const suppressReleaseClick = () => {
      suppressClick = true; document.addEventListener("click", swallowClick, true); clearTimeout(clickTimer);
      clickTimer = window.setTimeout(() => { suppressClick = false; document.removeEventListener("click", swallowClick, true); }, 350);
    };
    const snapshot = grid => { previousRects.current = new Map(Array.from(grid.querySelectorAll(CARD)).map(node => [node.dataset.cardId, node.getBoundingClientRect()])); };
    const publishPreview = (drag, ids) => {
      if (sameOrder(drag.preview, ids)) return;
      snapshot(drag.grid); drag.preview = ids;
      if (mounted.current) setPreviewIds(sameOrder(ids, drag.ids) ? null : ids);
    };
    const clearTarget = drag => { drag.target?.node.classList.remove("card-drop-active", "card-drop-saving", "card-drop-accepted"); drag.target = null; };
    const revealCard = drag => {
      if (drag.revealed || (!drag.overlay && !drag.card.classList.contains("card-drag-placeholder"))) return;
      drag.revealed = true;
      // Hand over from the opaque clone in the same frame. Fading the .18
      // placeholder after removing the clone produced a visible dark flash.
      drag.card.classList.add("card-drag-reveal");
      drag.card.classList.remove("card-drag-placeholder");
      drag.overlay?.remove(); drag.overlay = null;
      requestAnimationFrame(() => drag.card.classList.remove("card-drag-reveal"));
    };
    const cleanup = drag => {
      if (!drag || drag.phase === "disposed") return;
      cancelAnimationFrame(raf); drag.phase = "disposed"; drag.cancelAnimation?.(); clearTarget(drag);
      revealCard(drag); document.documentElement.classList.remove("account-card-dragging");
      try { drag.card.releasePointerCapture(drag.pointerId); } catch { /* Already released. */ }
      if (session.current === drag) session.current = null;
      if (mounted.current) { snapshot(drag.grid); setPreviewIds(null); setDraggingId(null); setDropTarget(null); }
    };
    const animateOverlay = async (drag, destination, disappear = false) => {
      if (!drag.overlay?.isConnected || reducedMotion() || !drag.overlay.animate) return;
      const from = drag.visual;
      const style = getComputedStyle(drag.overlay);
      const transform = style.transform && style.transform !== "none" ? style.transform : `translate3d(${from.x}px,${from.y}px,0) scale(${from.scale})`;
      const opacity = Number.parseFloat(style.opacity);
      const animation = drag.overlay.animate([
        { transform, opacity: Number.isFinite(opacity) ? opacity : .95 },
        { transform: `translate3d(${destination.x}px,${destination.y}px,0) scale(${destination.scale})`, opacity: disappear ? 0 : 1 },
      ], { duration: disappear ? 240 : 200, easing: "cubic-bezier(.22,.8,.24,1)", fill: "forwards" });
      drag.cancelAnimation = () => animation.cancel();
      try { await animation.finished; } catch { /* Unmount interrupted it. */ }
      drag.cancelAnimation = null;
    };
    const landWhileSaving = async drag => {
      if (!reducedMotion()) await new Promise(resolve => requestAnimationFrame(resolve));
      if (drag.phase === "disposed") return;
      const rect = layoutRect(drag.card);
      await animateOverlay(drag, { x: rect.left, y: rect.top, scale: 1 });
      if (drag.phase !== "disposed") revealCard(drag);
      // Keep the declarative preview until persistence confirms or rolls back.
    };
    const settle = async (drag, success, cancelled = false) => {
      if (drag.phase === "disposed") return;
      drag.phase = "settling";
      if (success && drag.target) {
        const rect = drag.target.node.isConnected ? drag.target.node.getBoundingClientRect() : drag.target.rect;
        drag.target.node.classList.remove("card-drop-saving"); drag.target.node.classList.add("card-drop-accepted");
        await animateOverlay(drag, { x: (rect.left + rect.right) / 2, y: (rect.top + rect.bottom) / 2, scale: .02 }, true);
      } else {
        if (!success) publishPreview(drag, drag.ids);
        if (!reducedMotion()) await new Promise(resolve => requestAnimationFrame(resolve));
        if (drag.phase === "disposed") return;
        const rect = layoutRect(drag.card); await animateOverlay(drag, { x: rect.left, y: rect.top, scale: 1 });
      }
      cleanup(drag); if (cancelled && mounted.current) setStatus("已取消移动。");
    };
    const cancel = (immediate = false) => {
      const drag = session.current;
      if (!drag || drag.phase === "saving" || drag.phase === "settling") return;
      cancelAnimationFrame(raf);
      if (drag.phase === "pending" || immediate) cleanup(drag);
      else { suppressReleaseClick(); void settle(drag, false, true); }
    };
    const readTargets = drag => Array.from(document.querySelectorAll(TARGET)).filter(node => !node.disabled && node.getAttribute("aria-disabled") !== "true"
      && !node.closest("[inert]") && (node.hasAttribute("data-card-pool-drop") || !["all", drag.sourceGroup].includes(node.dataset.cardGroupDrop)))
      .map(node => ({ node, type: node.hasAttribute("data-card-pool-drop") ? "apiPool" : "group", id: node.dataset.cardGroupDrop, rect: node.getBoundingClientRect() }));
    const frame = (schedule = true) => {
      const drag = session.current; if (drag?.phase !== "dragging") return;
      if (!drag.card.isConnected || gridRef.current !== drag.grid) { cancel(true); return; }
      const nodes = Array.from(drag.grid.querySelectorAll(CARD)), slots = nodes.map(layoutRect), gridRect = drag.grid.getBoundingClientRect();
      if (Math.abs(gridRect.width - drag.gridWidth) > 2) { cancel(true); return; }
      const point = { x: drag.x, y: drag.y }, { hit, near } = cardDropTarget(readTargets(drag), point, drag.target?.node);
      const scrolling = drag.scroller, scrollRect = scrolling === document.scrollingElement ? { top: 0, bottom: window.innerHeight } : scrolling.getBoundingClientRect();
      let payload = null, preview = drag.preview;
      if (hit) {
        preview = drag.ids; payload = { sourceId: drag.id, visibleIds: drag.ids, ...(hit.type === "apiPool" ? { dropTarget: "apiPool" } : { groupId: hit.id }) };
      } else if (containsPoint(gridRect, point, 10)) {
        drag.slot = gridSlotAt(slots, point, drag.slot); preview = moveCardToSlot(drag.ids, drag.id, drag.slot); payload = cardOrderPayload(drag.ids, preview, drag.id);
      }
      const visual = near ? dockCardPreview(near.rect, drag.width, drag.height, { width: window.innerWidth, height: window.innerHeight })
        : { x: drag.x - drag.offsetX, y: drag.y - drag.offsetY, scale: 1.018 };
      // Layout reads above; React preview, overlay and scrolling writes below.
      if (hit?.node !== drag.target?.node) {
        clearTarget(drag); drag.target = hit; hit?.node.classList.add("card-drop-active");
        if (mounted.current) setDropTarget(hit ? { type: hit.type, ...(hit.id ? { id: hit.id } : {}) } : null);
      }
      drag.payload = payload; drag.visual = visual; drag.overlay.classList.toggle("card-drag-docked", Boolean(near));
      drag.overlay.style.setProperty("--card-drag-x", `${visual.x}px`); drag.overlay.style.setProperty("--card-drag-y", `${visual.y}px`);
      drag.overlay.style.setProperty("--card-drag-scale", String(visual.scale)); publishPreview(drag, preview);
      const top = Math.max(0, scrollRect.top), bottom = Math.min(window.innerHeight, scrollRect.bottom), edge = 72;
      const speed = drag.y < top + edge ? -Math.min(1, (top + edge - drag.y) / edge) : drag.y > bottom - edge ? Math.min(1, (drag.y - bottom + edge) / edge) : 0;
      const now = performance.now(), elapsed = Math.min(32, now - drag.lastFrame); drag.lastFrame = now;
      if (schedule && speed && !hit) scrolling.scrollTop += speed * elapsed * .7;
      if (schedule) raf = requestAnimationFrame(() => frame());
    };
    const activate = drag => {
      if (latest.current.busy || committing.current || !drag.card.isConnected || gridRef.current !== drag.grid) { cleanup(drag); return; }
      const rect = layoutRect(drag.card); snapshot(drag.grid); const overlay = drag.card.cloneNode(true);
      overlay.removeAttribute("id"); overlay.removeAttribute("data-card-id"); overlay.querySelectorAll("[id]").forEach(node => node.removeAttribute("id"));
      overlay.setAttribute("aria-hidden", "true"); overlay.inert = true; overlay.classList.add("card-drag-overlay");
      overlay.style.width = `${rect.width}px`; overlay.style.height = `${rect.height}px`;
      drag.width = rect.width; drag.height = rect.height; drag.offsetX = drag.startX - rect.left; drag.offsetY = drag.startY - rect.top;
      drag.gridWidth = drag.grid.getBoundingClientRect().width; drag.overlay = overlay; drag.phase = "dragging"; drag.lastFrame = performance.now();
      drag.visual = { x: rect.left, y: rect.top, scale: 1 }; document.body.appendChild(overlay); drag.card.classList.add("card-drag-placeholder");
      document.documentElement.classList.add("account-card-dragging");
      try { drag.card.setPointerCapture(drag.pointerId); } catch { /* Document events still track drag. */ }
      window.getSelection()?.removeAllRanges(); setDraggingId(drag.id); setStatus("拖动调整位置，或移入分组与本地 API 号池；Escape 取消。"); frame();
    };
    const pointerDown = event => {
      if (session.current || latest.current.busy || committing.current || event.button !== 0 || event.pointerType !== "mouse" || !event.isPrimary) return;
      const grid = gridRef.current, card = event.target.closest?.(CARD);
      if (!grid || !card || !grid.contains(card) || !isCardBlankSpace(event.target, card, event)) return;
      const ids = idsOf(latest.current.items); if (!ids.includes(card.dataset.cardId)) return;
      let scroller = grid.parentElement;
      while (scroller && !(scroller.scrollHeight > scroller.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
      session.current = { grid, card, id: card.dataset.cardId, pointerId: event.pointerId, x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY,
        sourceGroup: card.dataset.cardGroupId || latest.current.groupId, viewGroup: latest.current.groupId,
        ids, preview: ids, slot: ids.indexOf(card.dataset.cardId), phase: "pending", scroller: scroller || document.scrollingElement };
    };
    const pointerMove = event => {
      const drag = session.current; if (!drag || !["pending", "dragging"].includes(drag.phase) || event.pointerId !== drag.pointerId) return;
      if (!(event.buttons & 1)) { cancel(); return; }
      drag.x = event.clientX; drag.y = event.clientY;
      if (drag.phase === "pending" && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) >= 4) activate(drag);
      if (drag.phase === "dragging") event.preventDefault();
    };
    const pointerUp = event => {
      const drag = session.current; if (!drag || event.pointerId !== drag.pointerId || !["pending", "dragging"].includes(drag.phase)) return;
      if (drag.phase === "pending") { cleanup(drag); return; }
      event.preventDefault(); event.stopPropagation(); suppressReleaseClick(); cancelAnimationFrame(raf);
      drag.x = event.clientX; drag.y = event.clientY; frame(false); if (drag.phase !== "dragging") return;
      drag.phase = "saving";
      try { drag.card.releasePointerCapture(drag.pointerId); } catch { /* Already released. */ }
      document.documentElement.classList.remove("account-card-dragging");
      if (!drag.payload) { void settle(drag, false, true); return; }
      if (drag.target) {
        drag.target.node.classList.add("card-drop-saving");
        void commitRef.current(drag.payload).then(success => settle(drag, success));
      } else {
        const landing = landWhileSaving(drag);
        void Promise.all([commitRef.current(drag.payload), landing]).then(([success]) => {
          if (success) cleanup(drag);
          else void settle(drag, false);
        });
      }
    };
    const pointerCancel = event => { if (event.pointerId === session.current?.pointerId) cancel(); };
    const captureLost = event => {
      const drag = session.current;
      if (!drag || drag.phase !== "dragging" || event.pointerId !== drag.pointerId) return;
      // React may move the keyed source with insertBefore during preview. That
      // DOM move can release capture even though the drag itself is still live.
      // Document listeners remain authoritative while the new layout commits.
      requestAnimationFrame(() => {
        if (session.current !== drag || drag.phase !== "dragging") return;
        if (!drag.card.isConnected || gridRef.current !== drag.grid) { cancel(true); return; }
        try { drag.card.setPointerCapture(drag.pointerId); } catch { /* Keep document tracking. */ }
      });
    };
    const keyDown = event => { if (event.key === "Escape" && session.current) { event.preventDefault(); cancel(); } };
    const visibility = () => { if (document.hidden) cancel(true); }, blur = () => cancel(true);
    cancelDrag.current = () => cancel(true);
    document.addEventListener("pointerdown", pointerDown, true); document.addEventListener("pointermove", pointerMove, { passive: false });
    document.addEventListener("pointerup", pointerUp, true); document.addEventListener("pointercancel", pointerCancel);
    document.addEventListener("lostpointercapture", captureLost, true); document.addEventListener("keydown", keyDown, true);
    document.addEventListener("visibilitychange", visibility); window.addEventListener("blur", blur);
    return () => {
      cleanup(session.current); cancelDrag.current = null;
      document.removeEventListener("pointerdown", pointerDown, true); document.removeEventListener("pointermove", pointerMove);
      document.removeEventListener("pointerup", pointerUp, true); document.removeEventListener("pointercancel", pointerCancel);
      document.removeEventListener("lostpointercapture", captureLost, true); document.removeEventListener("keydown", keyDown, true);
      document.removeEventListener("visibilitychange", visibility); window.removeEventListener("blur", blur);
    };
  }, [gridRef]);

  useEffect(() => {
    const drag = session.current;
    if (drag && ["pending", "dragging"].includes(drag.phase) && (busy || drag.viewGroup !== groupId || JSON.stringify(drag.ids) !== itemKey)) cancelDrag.current?.();
  }, [busy, groupId, itemKey]);
  return { draggingId, saving, status, previewIds, dropTarget,
    moveBy: (sourceId, delta) => session.current ? Promise.resolve(false) : commitRef.current(keyboardCardMove(idsOf(latest.current.items), sourceId, delta)) };
}
