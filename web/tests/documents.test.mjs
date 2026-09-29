import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { documentReference, linkedDocument } from "../.test-build/document-links.js";
import { DocumentContent } from "../.test-build/document-content.js";

const source = "E:\\exports\\one-run\\plan.md";

test("relative assets preserve Unicode and resolve against the exported document", () => {
  const reference = documentReference("frames/镜头%20一.png", source, "one-run", "v2");
  const url = new URL(reference.url, "http://localhost");
  assert.equal(url.pathname, "/api/file");
  assert.equal(url.searchParams.get("path"), "E:/exports/one-run/frames/镜头 一.png");
  assert.equal(url.searchParams.get("run"), "one-run");
  assert.equal(url.searchParams.get("version"), "v2");
  assert.equal(documentReference("E:\\work\\music.mp3", source, "one-run", "v2").filePath, "E:/work/music.mp3");
  const guide = documentReference("精剪指导.md", source, "one-run", "v2");
  assert.equal(linkedDocument(guide.url), "E:/exports/one-run/精剪指导.md");
  assert.equal(linkedDocument("https://example.com/api/file?path=other.md"), undefined);
});

test("document URLs cannot introduce executable schemes or network-share file paths", () => {
  for (const value of ["javascript:alert(1)", "javascript%3Aalert(1)", "data:image/svg+xml,evil", "vbscript:evil", "file:///etc/passwd", "//server/share", "\\\\server\\share", "java\nscript:evil", "%zz"]) {
    assert.equal(documentReference(value, source, "one-run", "0").url, "", value);
  }
  assert.equal(documentReference("mailto:a@example.com", source, "one-run", "0", true).url, "");
  assert.equal(documentReference("https://example.com/image.png", source, "one-run", "0", true).url,
    "https://example.com/image.png");
});

test("the real Markdown renderer displays tables, task lists and local images without raw HTML execution", () => {
  const text = `# 粗剪方案

![分镜](<frames/shot 1.png>)

| 旁白 | 区间 |
| --- | --- |
| 前半句 \\| 后半句 | 00:00:00:00 |

- [x] 核对画面

[指导](精剪指导.md)
[危险链接](javascript:alert%281%29)
<script>alert('unsafe')</script>
<img src=x onerror=alert('unsafe')>
`;
  const html = renderToStaticMarkup(React.createElement(DocumentContent, {
    text, path: source, run: "one-run", version: "v3",
  }));
  assert.match(html, /<h1>粗剪方案<\/h1>/);
  assert.match(html, /<table>/);
  assert.match(html, /<td>前半句 \| 后半句<\/td>/);
  assert.match(html, /type="checkbox"[^>]*checked=""/);
  assert.match(html, /src="\/api\/file\?path=E%3A%2Fexports%2Fone-run%2Fframes%2Fshot\+1.png/);
  assert.match(html, /version=v3/);
  assert.doesNotMatch(html, /<script|<[^>]+\sonerror=|href="javascript:/);
  assert.equal((html.match(/<img /g) ?? []).length, 1); // malformed HTML can appear as escaped text
  assert.match(html, /<span>危险链接<\/span>/);
});
