import "katex/dist/katex.min.css";
import { Check, Copy } from "lucide-react";
import { memo, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { cn } from "@/lib/format";

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

function CodeBlock({ children, className }: { children?: ReactNode; className?: string }) {
  const [copied, setCopied] = useState(false);
  const child = Array.isArray(children) ? children[0] : children;
  const codeClass = (child as { props?: { className?: string } })?.props?.className || "";
  const language = /language-([\w+-]+)/.exec(codeClass)?.[1] || "";
  return (
    <div className="group relative">
      <div className="pointer-events-none absolute top-2 right-2 flex items-center gap-2">
        {language && <span className="text-[11px] text-ink-3">{language}</span>}
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(textOf(children).replace(/\n$/, ""));
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
          className="pointer-events-auto rounded-md border border-line bg-surface p-1 text-ink-3 opacity-0 transition-opacity group-hover:opacity-100 hover:text-ink focus:opacity-100"
          aria-label="复制代码"
        >
          {copied ? <Check className="size-3.5 text-green" /> : <Copy className="size-3.5" />}
        </button>
      </div>
      <pre className={className}>{children}</pre>
    </div>
  );
}

/** Normalise model math delimiters \( \) and \[ \] into remark-math's $ / $$. */
function normalise(source: string): string {
  return source
    .replace(/\\\[([\s\S]+?)\\\]/g, (_m, body) => `\n$$\n${body}\n$$\n`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_m, body) => `$${body}$`);
}

export const Markdown = memo(function Markdown({ source, className }: { source: string; className?: string }) {
  return (
    <div className={cn("prose-answer", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }], rehypeKatex]}
        components={{
          pre: ({ children, className: preClass }) => <CodeBlock className={preClass}>{children}</CodeBlock>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
        }}
        urlTransform={(url) => (/^(https?:|mailto:|#|\/)/i.test(url) ? url : "")}
      >
        {normalise(source)}
      </ReactMarkdown>
    </div>
  );
});
