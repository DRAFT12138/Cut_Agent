"""Standalone, read-only browser view of the exported Markdown handoff."""
from __future__ import annotations

from html import escape
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin


_SECTIONS = (
    ("粗剪方案.md", "cut-plan", "粗剪方案"),
    ("精剪指导.md", "cut-guide", "精剪指导"),
    ("精剪核对记录.md", "cut-review", "核对记录"),
)
_TARGETS = {name: anchor for name, anchor, _ in _SECTIONS}

_STYLE = """
:root { color-scheme: dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; color: #d7e3f1; background: #0b1320; line-height: 1.7; }
a { color: #70c5ff; }
header { padding: 22px max(20px, calc((100vw - 1500px) / 2)); border-bottom: 1px solid #2c3e55; background: #111e30; }
header h1 { margin: 0; font-size: 25px; }
header p { margin: 4px 0 0; color: #a9bbcf; }
.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 24px; max-width: 1500px; margin: auto; padding: 20px; }
.directory { position: sticky; top: 20px; align-self: start; max-height: calc(100vh - 40px); overflow: auto; padding: 16px; border: 1px solid #30445d; border-radius: 8px; background: #122035; }
.directory strong { display: block; margin-bottom: 8px; }
.directory a { display: block; padding: 3px 0; text-decoration: none; overflow-wrap: anywhere; }
.directory a:hover, .directory a:focus-visible { text-decoration: underline; }
.directory .sub { padding-left: 14px; color: #b3d9f4; font-size: 13px; }
.mobile-directory { display: none; }
.mobile-directory nav a { display: block; padding: 3px 0; text-decoration: none; overflow-wrap: anywhere; }
.mobile-directory nav a.sub { padding-left: 14px; color: #b3d9f4; font-size: 13px; }
main { min-width: 0; }
.notice, .document { margin-bottom: 18px; padding: 20px 24px; border: 1px solid #30445d; border-radius: 8px; background: #122035; }
.notice { border-color: #725523; background: #292219; }
.notice p { margin: 0 0 8px; }
.document { overflow-wrap: anywhere; }
.document-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 16px; border-bottom: 1px solid #30445d; }
.document-title h2 { margin: 0 0 10px; font-size: 24px; }
.document-title a { font-size: 13px; }
.document h1 { font-size: 23px; }
.document h2 { margin: 30px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #30445d; font-size: 20px; }
.document h3 { font-size: 17px; }
.document img { display: block; max-width: 100%; height: auto; margin: 12px 0; border-radius: 6px; }
.document blockquote { margin: 12px 0; padding: 4px 14px; border-left: 3px solid #48b6ff; background: #182940; }
.document pre, .document table { display: block; max-width: 100%; overflow-x: auto; }
.document pre { padding: 12px; background: #0b1320; }
.document code { overflow-wrap: anywhere; }
.document th, .document td { padding: 6px 10px; border: 1px solid #39506b; }
.document th { text-align: left; background: #1c3049; }
.document input[type=checkbox] { margin-right: 8px; }
video { display: block; width: 100%; max-width: 960px; margin: 12px 0; }
@media (max-width: 700px) {
  header { padding: 14px 16px; }
  header h1 { font-size: 21px; }
  .layout { display: block; padding: 12px; }
  .directory { display: none; }
  .mobile-directory { display: block; margin-bottom: 12px; padding: 10px 14px; border: 1px solid #30445d; border-radius: 8px; background: #122035; }
  .mobile-directory nav { max-height: 50vh; overflow: auto; }
  .notice, .document { padding: 14px; }
}
"""


def _render_document(md: MarkdownIt, body: str, anchor: str) -> tuple[str, list[tuple[str, str]]]:
    tokens = md.parse(body)
    headings: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            if token.tag == "h1":
                token.attrSet("id", f"{anchor}-title")
            elif token.tag == "h2":
                section_anchor = f"{anchor}-section-{len(headings) + 1}"
                token.attrSet("id", section_anchor)
                headings.append((tokens[index + 1].content, section_anchor))
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                target = _TARGETS.get(unquote(urlsplit(href).path))
                if target:
                    child.attrSet("href", f"#{target}")
    return md.renderer.render(tokens, md.options, {}), headings


def build_html(documents: dict[str, str], run_id: str, revision: int, preview: bool) -> str:
    """Render a ZIP-root HTML page; generated documents remain the source of truth."""
    # js-default escapes raw HTML while retaining tables, images and links.
    md = MarkdownIt("js-default").use(tasklists_plugin)
    rendered = []
    directory = []
    for name, anchor, title in _SECTIONS:
        content, headings = _render_document(md, documents[name], anchor)
        directory.append(f'<a href="#{anchor}">{escape(title)}</a>')
        directory.extend(f'<a class="sub" href="#{item_anchor}">{escape(label)}</a>'
                         for label, item_anchor in headings)
        rendered.append(f'<section class="document" id="{anchor}">'
                        f'<div class="document-title"><h2>{escape(title)}</h2>'
                        f'<a href="{escape(name, quote=True)}">原始 Markdown</a></div>'
                        f'{content}</section>')
    links = "\n".join(directory)
    video = ('<video controls preload="metadata" src="preview.mp4" aria-label="粗剪预览"></video>'
             if preview else "")
    return ("<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" "
            "content=\"default-src 'none'; img-src 'self' file: data:; "
            "media-src 'self' file:; style-src 'unsafe-inline'\">"
            f"<title>精剪交接 · {escape(run_id)}</title><style>{_STYLE}</style></head><body>"
            f"<header><h1>精剪交接</h1><p>任务 {escape(run_id)} · 方案修订 {revision}</p></header>"
            '<div class="layout"><aside class="directory"><strong>交接目录</strong>'
            f'<nav aria-label="交接目录">{links}</nav></aside><main>'
            '<details class="mobile-directory"><summary>查看交接目录</summary>'
            f'<nav aria-label="移动端交接目录">{links}</nav></details>'
            '<section class="notice"><p>先核对粗剪方案的取材与时间码，再按精剪指导在达芬奇等软件中执行。'
            '原始素材、网络素材及配乐源文件需另行携带并重新定位。</p>'
            '<p>本页可离线阅读；人工核对状态请以包内《精剪核对记录.md》为准。</p>'
            f'{video}</section>{"".join(rendered)}</main></div></body></html>')
