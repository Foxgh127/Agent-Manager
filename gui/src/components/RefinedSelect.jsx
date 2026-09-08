import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, KeyRound, Route } from "lucide-react";
import "./RefinedSelect.css";

const MENU_GAP = 7;
const VIEWPORT_GUTTER = 8;
const TYPEAHEAD_RESET_MS = 650;
const MENU_TOKENS = ["--surface","--surface-2","--surface-3","--text","--muted","--muted-2","--accent","--accent-soft","--accent-rgb","--border","--border-soft","--glass","--glass-line","--glass-highlight","--shadow-float","--font-ui","--on-accent"];

function isSameValue(left, right) {
  return Object.is(left, right) || String(left ?? "") === String(right ?? "");
}

function firstEnabled(options) {
  return options.findIndex((option) => !option.disabled);
}

function lastEnabled(options) {
  for (let index = options.length - 1; index >= 0; index -= 1) {
    if (!options[index].disabled) return index;
  }
  return -1;
}

function nextEnabled(options, currentIndex, direction) {
  if (!options.some((option) => !option.disabled)) return -1;
  let index = currentIndex;
  for (let attempts = 0; attempts < options.length; attempts += 1) {
    index = (index + direction + options.length) % options.length;
    if (!options[index].disabled) return index;
  }
  return -1;
}

function menuPosition(trigger, variant, optionCount) {
  const rect = trigger.getBoundingClientRect();
  const viewport = window.visualViewport;
  const viewportLeft = viewport?.offsetLeft || 0;
  const viewportTop = viewport?.offsetTop || 0;
  const viewportWidth = viewport?.width || window.innerWidth;
  const viewportHeight = viewport?.height || window.innerHeight;
  const viewportRight = viewportLeft + viewportWidth;
  const viewportBottom = viewportTop + viewportHeight;
  const availableBelow = Math.max(0, viewportBottom - rect.bottom - MENU_GAP - VIEWPORT_GUTTER);
  const availableAbove = Math.max(0, rect.top - viewportTop - MENU_GAP - VIEWPORT_GUTTER);
  const estimatedHeight = Math.min(320, Math.max(52, optionCount * 50 + 10));
  const placeBelow = availableBelow >= Math.min(estimatedHeight, 180) || availableBelow >= availableAbove;
  const availableHeight = placeBelow ? availableBelow : availableAbove;
  const maxHeight = Math.max(0, Math.min(320, availableHeight));
  const availableWidth = Math.max(0, viewportWidth - VIEWPORT_GUTTER * 2);
  const preferredWidth = ["key","group"].includes(variant) ? Math.max(220, rect.width) : rect.width;
  const width = Math.max(0, Math.min(preferredWidth, availableWidth));
  const left = Math.min(
    Math.max(rect.left, viewportLeft + VIEWPORT_GUTTER),
    Math.max(viewportLeft + VIEWPORT_GUTTER, viewportRight - VIEWPORT_GUTTER - width),
  );
  const top = placeBelow
    ? rect.bottom + MENU_GAP
    : Math.max(viewportTop + VIEWPORT_GUTTER, rect.top - MENU_GAP - Math.min(estimatedHeight, maxHeight));

  const computed = getComputedStyle(trigger);
  const tokens = Object.fromEntries(MENU_TOKENS.map(key => [key,computed.getPropertyValue(key)]));
  return { left, top, bottom: window.innerHeight - rect.top + MENU_GAP, width, maxHeight, tokens, placement: placeBelow ? "bottom" : "top" };
}

export default function RefinedSelect({
  value,
  onChange,
  options: rawOptions,
  ariaLabel,
  disabled = false,
  variant = "route",
  placeholder = "请选择",
}) {
  const triggerId = useId();
  const listboxId = useId();
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const restoreFocusRef = useRef(false);
  const typeaheadRef = useRef({ text: "", timer: 0 });
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState(null);
  const options = Array.isArray(rawOptions) ? rawOptions : [];
  const selectedIndex = useMemo(
    () => options.findIndex((option) => isSameValue(option.value, value)),
    [options, value],
  );
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : null;
  const isKeyVariant = variant === "key";
  const isGroupVariant = variant === "group";
  const isFieldVariant = variant === "field";
  const TriggerIcon = isKeyVariant ? KeyRound : Route;

  const clearTypeahead = useCallback(() => {
    window.clearTimeout(typeaheadRef.current.timer);
    typeaheadRef.current = { text: "", timer: 0 };
  }, []);

  const closeMenu = useCallback((restoreFocus = false) => {
    setOpen(false);
    clearTypeahead();
    if (restoreFocus) {
      restoreFocusRef.current = true;
      window.requestAnimationFrame(() => {
        if (triggerRef.current && !triggerRef.current.disabled) {
          triggerRef.current.focus({ preventScroll: true });
          restoreFocusRef.current = false;
        }
      });
    }
  }, [clearTypeahead]);

  const openMenu = useCallback((preferredIndex = selectedIndex) => {
    if (disabled || !options.some((option) => !option.disabled)) return;
    const nextIndex = preferredIndex >= 0 && !options[preferredIndex]?.disabled
      ? preferredIndex
      : firstEnabled(options);
    setActiveIndex(nextIndex);
    setOpen(true);
  }, [disabled, options, selectedIndex]);

  const choose = useCallback((index) => {
    const option = options[index];
    if (!option || option.disabled) return;
    if (!isSameValue(option.value, value)) onChange?.(option.value);
    closeMenu(true);
  }, [closeMenu, onChange, options, value]);

  const moveActive = useCallback((direction) => {
    setActiveIndex((current) => nextEnabled(options, current, direction));
  }, [options]);

  const handleTypeahead = useCallback((key) => {
    const character = key.toLocaleLowerCase();
    const previous = typeaheadRef.current.text;
    const repeatedCharacter = previous && [...previous].every((part) => part === character);
    const text = repeatedCharacter ? character : `${previous}${character}`;
    window.clearTimeout(typeaheadRef.current.timer);
    typeaheadRef.current = {
      text,
      timer: window.setTimeout(clearTypeahead, TYPEAHEAD_RESET_MS),
    };

    const start = repeatedCharacter ? activeIndex + 1 : 0;
    for (let offset = 0; offset < options.length; offset += 1) {
      const index = (start + offset) % options.length;
      const option = options[index];
      if (!option.disabled && String(option.label ?? "").trim().toLocaleLowerCase().startsWith(text)) {
        setActiveIndex(index);
        setOpen(true);
        return;
      }
    }
  }, [activeIndex, clearTypeahead, options]);

  const handleKeyDown = (event) => {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openMenu();
      else moveActive(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Home" && open) {
      event.preventDefault();
      setActiveIndex(firstEnabled(options));
      return;
    }
    if (event.key === "End" && open) {
      event.preventDefault();
      setActiveIndex(lastEnabled(options));
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open && activeIndex >= 0) choose(activeIndex);
      else openMenu();
      return;
    }
    if (event.key === "Tab" && open) {
      closeMenu();
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      handleTypeahead(event.key);
    }
  };

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) {
      setPosition(null);
      return undefined;
    }
    const update = () => {
      if (triggerRef.current) setPosition(menuPosition(triggerRef.current, variant, options.length));
    };
    update();
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(update);
    resizeObserver?.observe(triggerRef.current);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
    };
  }, [open, options.length, variant]);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event) => {
      if (!triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        closeMenu();
      }
    };
    const handleFocusIn = (event) => {
      if (!triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        closeMenu();
      }
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("focusin", handleFocusIn);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("focusin", handleFocusIn);
    };
  }, [closeMenu, open]);

  useEffect(() => {
    if (!open) return undefined;
    const handleEscape = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      closeMenu(true);
    };
    // Window capture runs before Dialog's document-level Escape handler. This
    // lets the select close first when it is later used inside a modal.
    window.addEventListener("keydown", handleEscape, true);
    return () => window.removeEventListener("keydown", handleEscape, true);
  }, [closeMenu, open]);

  useEffect(() => {
    if (!open || typeof MutationObserver === "undefined") return undefined;
    const closeWhenInert = () => {
      if (!triggerRef.current?.isConnected || triggerRef.current.closest("[inert]")) closeMenu();
    };
    const observer = new MutationObserver(closeWhenInert);
    observer.observe(document.body, { attributes: true, attributeFilter: ["inert"], subtree: true });
    closeWhenInert();
    return () => observer.disconnect();
  }, [closeMenu, open]);

  useEffect(() => {
    if (!open || activeIndex < 0 || !position) return;
    const list = menuRef.current?.querySelector(".refined-select-listbox");
    const option = list?.querySelector(`[data-refined-select-index="${activeIndex}"]`);
    if (!list || !option) return;
    const top = option.offsetTop;
    const bottom = top + option.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }, [activeIndex, open, position?.maxHeight]);

  useEffect(() => {
    if (!open) return;
    if (disabled || !options.some((option) => !option.disabled)) {
      closeMenu();
      return;
    }
    if (activeIndex < 0 || activeIndex >= options.length || options[activeIndex]?.disabled) {
      setActiveIndex(selectedIndex >= 0 && !options[selectedIndex]?.disabled
        ? selectedIndex
        : firstEnabled(options));
    }
  }, [activeIndex, closeMenu, disabled, open, options, selectedIndex]);

  useEffect(() => () => clearTypeahead(), [clearTypeahead]);
  useEffect(() => {
    if (!disabled && restoreFocusRef.current && triggerRef.current) {
      if (document.activeElement === document.body || document.activeElement === triggerRef.current) triggerRef.current.focus({ preventScroll: true });
      restoreFocusRef.current = false;
    }
  }, [disabled]);

  const activeDescendant = open && activeIndex >= 0
    ? `${listboxId}-option-${activeIndex}`
    : undefined;
  const menu = open && typeof document !== "undefined" ? createPortal(
    <div
      ref={menuRef}
      className="refined-select-menu"
      data-placement={position?.placement || "bottom"}
      style={position ? {
        ...position.tokens,
        left: position.left,
        top: position.placement === "bottom" ? position.top : undefined,
        bottom: position.placement === "top" ? position.bottom : undefined,
        width: position.width,
        maxHeight: position.maxHeight,
        "--refined-select-max-height": `${position.maxHeight}px`,
      } : { visibility: "hidden" }}
    >
      <div
        id={listboxId}
        className="refined-select-listbox"
        role="listbox"
        aria-labelledby={triggerId}
      >
        {options.length ? options.map((option, index) => {
          const selected = isSameValue(option.value, value);
          const active = index === activeIndex;
          return (
            <div
              key={`${String(option.value)}-${index}`}
              id={`${listboxId}-option-${index}`}
              data-refined-select-index={index}
              className={`refined-select-option${selected ? " is-selected" : ""}${active ? " is-active" : ""}${option.disabled ? " is-disabled" : ""}`}
              role="option"
              aria-selected={selected}
              aria-disabled={option.disabled || undefined}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => {
                if (!option.disabled) setActiveIndex(index);
              }}
              onClick={() => choose(index)}
            >
              <span className="refined-select-check" aria-hidden="true">
                {selected && <Check size={15} strokeWidth={2.5} />}
              </span>
              <span className="refined-select-option-label">{option.tone && <i className={`refined-select-dot refined-select-tone-${option.tone}`} aria-hidden="true" />}{option.label}</span>
              {option.detail && <small>{option.detail}</small>}
            </div>
          );
        }) : (
          <div className="refined-select-empty">没有可选项</div>
        )}
      </div>
    </div>,
    document.body,
  ) : null;

  return (
    <span className={`refined-select refined-select--${isGroupVariant ? "group" : isFieldVariant ? "field" : isKeyVariant ? "key" : "route"} refined-select-tone-${selectedOption?.tone || "slate"}${open ? " is-open" : ""}`}>
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        className="refined-select-trigger"
        role="combobox"
        aria-label={ariaLabel || placeholder}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={activeDescendant}
        aria-autocomplete="list"
        disabled={disabled}
        onClick={() => (open ? closeMenu() : openMenu())}
        onKeyDown={handleKeyDown}
      >
        {isGroupVariant ? <i className="refined-select-dot" aria-hidden="true" /> : !isFieldVariant && <TriggerIcon className="refined-select-leading" size={isKeyVariant ? 14 : 16} aria-hidden="true" />}
        <span className="refined-select-value">
          <strong>{selectedOption?.label ?? placeholder}</strong>
          {!isKeyVariant && !isGroupVariant && selectedOption?.detail && <small>{selectedOption.detail}</small>}
        </span>
        <ChevronDown className="refined-select-chevron" size={15} aria-hidden="true" />
      </button>
      {menu}
    </span>
  );
}
