import { useEffect, useState } from "react";
import { Alert, Button, Card, Space, Spin } from "antd";
import { api } from "./api";
import { DocumentContent } from "./document-content";
import "./documents.css";

export function DocumentViewer({ path, run, version }: { path: string; run: string; version: string }) {
  const [selected, setSelected] = useState(path);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => { setSelected(path); }, [path]);
  useEffect(() => {
    const request = new AbortController();
    setText(null);
    setError("");
    fetch(`${api.fileUrl(selected, run)}&version=${encodeURIComponent(version)}`, { signal: request.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`文档暂不可用（${response.status}）`);
        return response.text();
      })
      .then((value) => { if (!request.signal.aborted) setText(value); })
      .catch((reason) => { if (!request.signal.aborted) setError(String(reason)); });
    return () => request.abort();
  }, [selected, run, version, retry]);
  const filename = selected.replace(/\\/g, "/").split("/").pop();
  return <Card id="rough-cut-document" size="small" title={filename} extra={selected !== path
    ? <Button size="small" onClick={() => setSelected(path)}>返回粗剪方案</Button> : undefined}>
    {error ? <Alert type="warning" showIcon message={error}
      action={<Button size="small" onClick={() => setRetry((value) => value + 1)}>重试</Button>} />
      : text === null ? <Space><Spin size="small" />正在读取方案…</Space>
      : <DocumentContent text={text} path={selected} run={run} version={version} onDocument={setSelected} />}
  </Card>;
}
