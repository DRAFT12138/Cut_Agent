import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { documentReference, linkedDocument } from "./document-links.js";

export function DocumentContent({ text, path, run, version, onDocument }: {
  text: string; path: string; run: string; version: string;
  onDocument?: (path: string) => void;
}) {
  const contentRef = useRef<HTMLElement>(null);
  const jumpUntil = useRef(0);
  const [sections, setSections] = useState<string[]>([]);
  const [activeSection, setActiveSection] = useState(-1);
  useEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    setSections(Array.from(content.querySelectorAll("h2"), (heading) => heading.textContent?.trim() || "未命名章节"));
    setActiveSection(-1);
  }, [text, path]);
  const headings = () => Array.from(contentRef.current?.querySelectorAll("h2") ?? []);
  const jumpTo = (index: number) => {
    const content = contentRef.current;
    const heading = headings()[index];
    if (!content || !heading) return;
    const top = content.scrollTop + heading.getBoundingClientRect().top - content.getBoundingClientRect().top - 8;
    jumpUntil.current = Date.now() + 200;
    content.scrollTo({ top, behavior: "auto" });
    setActiveSection(index);
  };
  const updateSection = () => {
    if (Date.now() < jumpUntil.current) return;
    const content = contentRef.current;
    if (!content) return;
    const items = headings();
    const threshold = content.getBoundingClientRect().top + 24;
    let current = -1;
    items.forEach((heading, index) => { if (heading.getBoundingClientRect().top <= threshold) current = index; });
    if (content.scrollTop + content.clientHeight >= content.scrollHeight - 2) current = items.length - 1;
    setActiveSection(current);
  };
  return <div className="plan-reader">
    {sections.length > 1 && <nav className="plan-sections" aria-label="文档章节">
      <span className="plan-sections-label">快速跳转</span>
      {sections.map((section, index) => <button key={`${section}-${index}`} type="button"
        aria-current={activeSection === index ? "location" : undefined}
        onClick={() => jumpTo(index)}>{section}</button>)}
    </nav>}
    <article className="plan-document" ref={contentRef} onScroll={updateSection} aria-label="方案正文">
    <Markdown skipHtml remarkPlugins={[remarkGfm]}
      urlTransform={(url, key) => documentReference(url, path, run, version, key === "src").url}
      components={{
        img: ({ src, alt, title }) => src
          ? <img src={src} alt={alt ?? "分镜画面"} title={title} loading="lazy" />
          : <span>图片链接不可用</span>,
        a: ({ href, children, title }) => {
          if (!href) return <span>{children}</span>;
          const next = linkedDocument(href);
          return <a href={href} title={title}
            target={href.startsWith("#") || (next && onDocument) ? undefined : "_blank"}
            rel="noreferrer"
            onClick={next && onDocument ? (event) => { event.preventDefault(); onDocument(next); } : undefined}>
            {children}
          </a>;
        },
      }}>{text}</Markdown>
    </article>
  </div>;
}
