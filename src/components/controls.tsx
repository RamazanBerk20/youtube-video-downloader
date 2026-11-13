// Small form controls shared by the main surface and the settings sheet.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "./icons";

export function Field({ label, children, hint, wide }: { label: ReactNode; children: (id: string) => ReactNode; hint?: ReactNode; wide?: boolean }) {
  const id = useId();
  return (
    <div className={wide ? "field wide" : "field"}>
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <p className="hint">{hint}</p>}
    </div>
  );
}

export function Select<T extends string>({
  id,
  value,
  options,
  label,
  onChange,
  disabled,
  testId
}: {
  id?: string;
  value: T;
  options: readonly T[];
  label: (v: T) => string;
  onChange: (v: T) => void;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <span className="select">
      <select id={id} value={value} disabled={disabled} data-testid={testId} onChange={(e) => onChange(e.target.value as T)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {label(o)}
          </option>
        ))}
      </select>
      <Icon name="chevronDown" size={14} />
    </span>
  );
}

export function Check({ checked, onChange, children, testId, disabled }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode; testId?: string; disabled?: boolean }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} data-testid={testId} onChange={(e) => onChange(e.target.checked)} />
      <span>{children}</span>
    </label>
  );
}

/** Text input that keeps its own buffer and commits on blur or Enter (item 97). */
export function TextSetting({
  id,
  value,
  onCommit,
  placeholder,
  type = "text",
  testId,
  disabled,
  dirtyOnly,
  dir
}: {
  id?: string;
  value: string;
  /** "ltr" for file paths so they read correctly in an RTL layout. */
  dir?: "ltr" | "auto";
  onCommit: (v: string) => void;
  placeholder?: string;
  type?: "text" | "password";
  testId?: string;
  disabled?: boolean;
  /** Commit only after the user typed (write-only password). */
  dirtyOnly?: boolean;
}) {
  const [text, setText] = useState(value);
  const dirty = useRef(false);
  useEffect(() => {
    if (!dirty.current) setText(value);
  }, [value]);
  // Reads the element, not the state, so a commit right after typing never sees a stale value.
  const commit = (current: string) => {
    if (dirtyOnly && !dirty.current) return;
    dirty.current = false;
    if (dirtyOnly || current !== value) onCommit(current);
    if (dirtyOnly) setText("");
  };
  return (
    <input
      id={id}
      type={type}
      dir={dir}
      value={text}
      placeholder={placeholder}
      disabled={disabled}
      data-testid={testId}
      spellCheck={false}
      autoComplete="off"
      onChange={(e) => {
        dirty.current = true;
        setText(e.target.value);
      }}
      onBlur={(e) => commit(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit(e.currentTarget.value);
      }}
    />
  );
}

/**
 * Editable combobox (items 19, 22, 58): free text plus a preset list. The text
 * is committed on blur/Enter and re-displayed from the parsed setting.
 */
export function Combo({
  id,
  value,
  presets,
  onCommit,
  testId,
  label
}: {
  id?: string;
  value: string;
  presets: readonly string[];
  onCommit: (text: string) => void;
  testId?: string;
  label: string;
}) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const editing = useRef(false);
  const root = useRef<HTMLSpanElement>(null);
  const listId = useId();
  useEffect(() => {
    if (!editing.current) setText(value);
  }, [value]);

  const commit = (v: string) => {
    editing.current = false;
    setOpen(false);
    onCommit(v);
    setText(value); // the parsed value arrives through `value` right after
  };

  return (
    <span
      className="combo"
      ref={root}
      onBlur={(e) => {
        if (!root.current?.contains(e.relatedTarget as Node)) {
          if (editing.current) commit(root.current?.querySelector("input")?.value ?? text);
          setOpen(false);
        }
      }}
    >
      <input
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        data-testid={testId}
        value={text}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => {
          editing.current = true;
          setText(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
            setActive((a) => Math.min(presets.length - 1, a + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => Math.max(0, a - 1));
          } else if (e.key === "Enter") {
            e.preventDefault();
            commit(open && active >= 0 ? presets[active] : e.currentTarget.value);
          } else if (e.key === "Escape" && open) {
            e.stopPropagation();
            setOpen(false);
          }
        }}
      />
      <button type="button" className="combo-toggle" tabIndex={-1} aria-label={label} onClick={() => setOpen((o) => !o)}>
        <Icon name="chevronDown" size={14} />
      </button>
      {open && (
        <ul className="combo-list" id={listId} role="listbox">
          {presets.map((p, i) => (
            <li
              key={p}
              role="option"
              aria-selected={p === value}
              className={i === active ? "active" : undefined}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => commit(p)}
            >
              {p}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}
