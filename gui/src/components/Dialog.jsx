import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, ShieldCheck, Trash2, X } from "lucide-react";

const surfaces = [];
let lastFocusedControl = null;
if (typeof document !== "undefined") document.addEventListener("focusin", event => {
  if (event.target instanceof HTMLElement && event.target !== document.body && event.target !== document.documentElement) lastFocusedControl = event.target;
});
let previousBodyOverflow = "";
let previousRootInert = false;
const focusableSelector = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
const focusable = node => [...node.querySelectorAll(focusableSelector)].filter(element => !element.closest('[inert]') && element.getClientRects().length);

function syncSurfaces() {
  surfaces.forEach((entry, index) => { entry.node.inert = index !== surfaces.length - 1; });
}

function useDialogSurface(ref, onEscape) {
  const escapeRef = useRef(onEscape);
  const openerRef = useRef(typeof document === "undefined" ? null : document.activeElement === document.body ? lastFocusedControl : document.activeElement);
  escapeRef.current = onEscape;
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const previousFocus = openerRef.current;
    const root = document.getElementById("root");
    if (!surfaces.length) {
      previousBodyOverflow = document.body.style.overflow;
      previousRootInert = Boolean(root?.inert);
      document.body.style.overflow = "hidden";
      if (root) root.inert = true;
    }
    const entry = { node };
    surfaces.push(entry);
    syncSurfaces();
    if (!node.contains(document.activeElement)) (focusable(node)[0] || node).focus({ preventScroll: true });
    const keydown = event => {
      if (surfaces.at(-1) !== entry) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        escapeRef.current?.();
      }
      if (event.key !== "Tab") return;
      const controls = focusable(node);
      const first = controls[0] || node;
      const last = controls.at(-1) || node;
      if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    const focusin = event => {
      if (surfaces.at(-1) === entry && !node.contains(event.target)) (focusable(node)[0] || node).focus();
    };
    document.addEventListener("keydown", keydown, true);
    document.addEventListener("focusin", focusin);
    return () => {
      document.removeEventListener("keydown", keydown, true);
      document.removeEventListener("focusin", focusin);
      const index = surfaces.indexOf(entry);
      if (index !== -1) surfaces.splice(index, 1);
      syncSurfaces();
      if (!surfaces.length) {
        document.body.style.overflow = previousBodyOverflow;
        if (root) root.inert = previousRootInert;
      }
      if (lastFocusedControl && node.contains(lastFocusedControl)) lastFocusedControl = previousFocus?.isConnected ? previousFocus : null;
      if (previousFocus?.isConnected && !previousFocus.closest?.('[inert]')) previousFocus.focus?.({ preventScroll: true });
      else if (surfaces.length) (focusable(surfaces.at(-1).node)[0] || surfaces.at(-1).node).focus({ preventScroll: true });
    };
  }, [ref]);
}

export function Modal({ title, description, children, onClose, wide = false, className = "", dismissible = true }) {
  const titleId = useId();
  const descriptionId = useId();
  const ref = useRef(null);
  useDialogSurface(ref, dismissible ? onClose : null);
  return createPortal(
    <div className="modal-backdrop" role="presentation">
      <section ref={ref} tabIndex={-1} className={`modal ${wide ? "wide" : ""} ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined}>
        <header className="modal-header">
          <div><h2 id={titleId}>{title}</h2>{description && <p id={descriptionId}>{description}</p>}</div>
          {dismissible && <button type="button" className="icon-button" aria-label="关闭" title="关闭" onClick={onClose}><X size={20} /></button>}
        </header>
        {children}
      </section>
    </div>, document.body,
  );
}

function ActiveConfirmation({ dialog, onResolve }) {
  const ref = useRef(null);
  const titleId = useId();
  const messageId = useId();
  const cancel = () => onResolve(dialog.choiceMode ? "cancel" : false);
  useDialogSurface(ref, cancel);
  const tone = dialog.tone || "warning";
  return createPortal(
    <div className="modal-backdrop confirm-backdrop" role="presentation">
      <section ref={ref} tabIndex={-1} className={`confirm-dialog tone-${tone}`} role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={messageId}>
        <span className="confirm-icon">{tone === "danger" ? <Trash2 size={25} /> : tone === "info" ? <ShieldCheck size={25} /> : <AlertTriangle size={25} />}</span>
        <div className="confirm-copy"><span className="confirm-eyebrow">{dialog.eyebrow || (tone === "danger" ? "需要确认" : "请确认操作")}</span><h2 id={titleId}>{dialog.title}</h2><p id={messageId}>{dialog.message}</p>{dialog.detail && <small>{dialog.detail}</small>}</div>
        <div className="confirm-actions">
          <button className="button secondary" autoFocus onClick={cancel}>{dialog.cancelLabel || "取消"}</button>
          {dialog.choiceMode && <button className="button danger-outline" onClick={() => onResolve("discard")}>{dialog.discardLabel || "放弃修改"}</button>}
          <button className={`button ${tone === "danger" ? "danger" : "primary"}`} onClick={() => onResolve(dialog.choiceMode ? "save" : true)}>{dialog.confirmLabel || "确认"}</button>
        </div>
      </section>
    </div>, document.body,
  );
}

export function ConfirmDialog(props) {
  return props.dialog ? <ActiveConfirmation key={props.dialog.id} {...props} onResolve={answer => props.onResolve(answer, props.dialog.id)} /> : null;
}
