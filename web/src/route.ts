export function replaceRunLocation(key: "stage" | "pane", value?: string) {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("run")) return;
  if (value) url.searchParams.set(key, value);
  else url.searchParams.delete(key);
  window.history.replaceState(null, "", url);
}
