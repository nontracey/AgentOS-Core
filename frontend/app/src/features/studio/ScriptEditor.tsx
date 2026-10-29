import { useLayoutEffect, useMemo, useRef, type ReactNode } from "react";
import { cn } from "@/lib/format";
import type { Character } from "@/lib/types";
import { characterColor } from "./labels";

/*
  Screenplay editor: a plain textarea over a highlighted copy of the same text.
  Both layers share one box model (font, size, line height, padding, wrapping), so only colour
  and backgrounds change — never widths — and the caret stays aligned with what you see.
*/

const BOX = "px-1 py-2 text-[15px] leading-[1.95] tracking-normal whitespace-pre-wrap break-words [overflow-wrap:anywhere]";

const DIALOGUE = /^([^：:【#\s][^：:（(]{0,15})([（(][^）)]{0,12}[）)])?([：:])(.*)$/;

/** Speaker colour: exact cast name first, then the longest cast name inside the label ("店员小林" → 小林). */
function colorFor(name: string, cast: Character[]): string | undefined {
  let index = cast.findIndex((c) => c.name === name);
  if (index < 0) {
    let best = 0;
    cast.forEach((c, i) => {
      if (c.name && c.name.length > best && name.includes(c.name)) {
        best = c.name.length;
        index = i;
      }
    });
  }
  if (index < 0) return undefined;
  return cast[index].color || characterColor(index);
}

function Line({ text, cast, tail }: { text: string; cast: Character[]; tail?: ReactNode }): ReactNode {
  if (!text)
    return (
      <div>
        {"\u200b"}
        {tail}
      </div>
    );
  if (text.startsWith("## ") || text.startsWith("##")) {
    const body = text.replace(/^##\s?/, "");
    return (
      <div className="-mx-1 rounded-[6px] bg-surface-3 px-1 text-ink">
        <span className="text-ink-3/70">{text.slice(0, text.length - body.length)}</span>
        {body}
        {tail}
      </div>
    );
  }
  if (text.startsWith("# ") || text.startsWith("#")) {
    const body = text.replace(/^#\s?/, "");
    return (
      <div className="text-accent-ink">
        <span className="text-ink-3/70">{text.slice(0, text.length - body.length)}</span>
        {body}
        {tail}
      </div>
    );
  }
  const action = text.match(/^(【[^】]{1,6}】)(.*)$/);
  if (action) {
    return (
      <div className="text-ink-2">
        <span className="text-blue">{action[1]}</span>
        {action[2]}
        {tail}
      </div>
    );
  }
  const dialogue = text.match(DIALOGUE);
  if (dialogue) {
    const [, name, emotion = "", colon, words] = dialogue;
    const color = colorFor(name.trim(), cast);
    return (
      <div>
        <span className={cn(!color && "text-ink")} style={color ? { color } : undefined}>
          {name}
        </span>
        <span className="text-ink-3">
          {emotion}
          {colon}
        </span>
        <span className="text-ink">{words}</span>
        {tail}
      </div>
    );
  }
  return (
    <div className="text-ink">
      {text}
      {tail}
    </div>
  );
}

export function ScriptHighlight({ text, cast, className, children }: { text: string; cast: Character[]; className?: string; children?: ReactNode }) {
  const lines = useMemo(() => text.split("\n"), [text]);
  return (
    <div className={cn(BOX, className)}>
      {lines.map((line, index) => (
        <Line key={index} text={line} cast={cast} tail={index === lines.length - 1 ? children : undefined} />
      ))}
    </div>
  );
}

export function ScriptEditor({
  value,
  onChange,
  cast,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  cast: Character[];
  placeholder?: string;
}) {
  const area = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <div className="relative min-h-[70vh]">
      <ScriptHighlight text={value} cast={cast} className="pointer-events-none absolute inset-0 select-none" />
      <textarea
        ref={area}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        aria-label="剧本"
        className={cn(
          BOX,
          "relative block min-h-[70vh] w-full resize-none overflow-hidden bg-transparent text-transparent caret-[var(--ink)] outline-none placeholder:text-ink-3 selection:bg-[rgb(233_169_58/0.28)] selection:text-transparent",
        )}
      />
    </div>
  );
}
