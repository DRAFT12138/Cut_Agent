export interface DocumentReference {
  url: string;
  filePath?: string;
}

/** Resolve exported relative assets against the document, never the SPA URL. */
export function documentReference(
  value: string, documentPath: string, run: string, version: string, image = false,
): DocumentReference {
  let target: string;
  try { target = decodeURIComponent(value.trim()); } catch { return { url: "" }; }
  if (!target || /[\u0000-\u001f\u007f]/.test(target)) return { url: "" };
  if (target.startsWith("#")) return image ? { url: "" } : { url: target };
  if (/^https?:\/\//i.test(target)) {
    try { return { url: new URL(value).href }; } catch { return { url: "" }; }
  }
  if (!image && /^mailto:/i.test(target)) return { url: target };
  target = target.replace(/\\/g, "/");
  // Explicit local drive paths are supported, but executable/data/file schemes
  // and network shares are not document assets. The API enforces allowed roots.
  if (target.startsWith("//")) return { url: "" };
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) && !/^[a-z]:\//i.test(target)) return { url: "" };
  const absolute = /^[a-z]:\//i.test(target) || target.startsWith("/");
  const directory = documentPath.replace(/\\/g, "/").replace(/[^/]+$/, "");
  const filePath = absolute ? target : directory + target;
  const query = new URLSearchParams({ path: filePath, run, version });
  return { url: `/api/file?${query}`, filePath };
}

/** Only links produced by documentReference may switch the embedded reader. */
export function linkedDocument(url: string): string | undefined {
  if (!url.startsWith("/api/file?")) return undefined;
  const parsed = new URL(url, "http://local.invalid");
  const path = parsed.searchParams.get("path");
  return path && /\.md$/i.test(path) ? path : undefined;
}
