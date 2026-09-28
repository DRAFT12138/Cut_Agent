import { useEffect, useRef, useState } from "react";
import { App, Button, Card, Space, Typography } from "antd";
import "./preview.css";

export type PreviewInfo = {
  status: string;
  error?: string;
  has_music?: boolean;
  markers?: { seq: number; start: number; end: number; media: string; missing: boolean }[];
};

export function PreviewPlayer({ runId, preview, revision, onChanged, onEditRow }: {
  runId: string; preview?: PreviewInfo | null; revision?: number;
  onChanged: () => void; onEditRow?: (seq: number) => void;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [busy, setBusy] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const { message } = App.useApp();
  useEffect(() => { setCurrentTime(0); }, [revision]);
  const markers = (preview?.markers ?? []).filter((marker) =>
    Number.isFinite(marker.start) && Number.isFinite(marker.end) && marker.end > marker.start);
  const total = Math.max(0, ...markers.map((marker) => marker.end));
  const active = markers.find((marker) => marker.start <= currentTime && currentTime < marker.end)
    ?? (currentTime >= total ? markers[markers.length - 1] : undefined);
  const seek = (start: number) => {
    if (video.current) video.current.currentTime = start;
    setCurrentTime(start);
  };
  const generate = async () => {
    setBusy(true);
    try {
      const response = await fetch(`/api/runs/${runId}/preview`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "预览生成失败");
      onChanged();
      if (data.preview?.status === "failed") message.error(data.preview.error);
      else message.success("预览已更新");
    } catch (error) { message.error(String(error)); }
    finally { setBusy(false); }
  };
  return <Card id="preview-player" size="small" title="粗剪预览（可选）"
    extra={<Button loading={busy} onClick={() => void generate()}>生成 / 更新预览</Button>}>
    {preview?.status === "ready" ? <Space direction="vertical" style={{ width: "100%" }}>
      <video key={revision ?? 0} ref={video} controls preload="metadata"
        onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
        onSeeked={(event) => setCurrentTime(event.currentTarget.currentTime)}
        src={`/api/runs/${runId}/preview?revision=${revision ?? 0}`}
        style={{ width: "100%", maxHeight: 480, background: "black" }} />
      {markers.length > 0 && <div className="preview-timeline" role="group" aria-label="预览段落导航">
        {total > 0 && <div className="preview-timeline__progress" aria-hidden="true">
          <div style={{ width: `${Math.min(100, Math.max(0, currentTime / total * 100))}%` }} />
        </div>}
        <div className="preview-timeline__segments">
          {markers.map((marker) => <button key={marker.seq} type="button"
            className={`preview-timeline__segment${active?.seq === marker.seq ? " is-active" : ""}${marker.missing ? " is-missing" : ""}`}
            style={{ flexGrow: marker.end - marker.start }}
            title={`第 ${marker.seq} 段 · ${marker.media} · ${marker.start.toFixed(2)}–${marker.end.toFixed(2)} 秒${marker.missing ? " · 素材待补" : ""}`}
            aria-label={`跳到第 ${marker.seq} 段，${marker.start.toFixed(2)} 秒${marker.missing ? "，素材待补" : ""}`}
            aria-current={active?.seq === marker.seq ? "true" : undefined}
            onClick={() => seek(marker.start)}>
            <span>#{marker.seq}</span><small>{marker.missing ? "待补" : marker.media}</small>
          </button>)}
        </div>
        <div className="preview-timeline__caption">
          <span>{active ? `当前第 ${active.seq} 段 · ${active.media}${active.missing ? "（素材待补）" : ""}` : "点击段落跳转"}</span>
          <span>{currentTime.toFixed(2)} / {total.toFixed(2)} 秒</span>
        </div>
        {active && onEditRow && <Button size="small" style={{ marginTop: 8 }}
          onClick={() => onEditRow(active.seq)}>调整当前第 {active.seq} 行</Button>}
      </div>}
      <Typography.Text type="secondary">{preview.has_music ? "已混入配乐" : "无配乐，预览无声"}；段号对应上方方案。</Typography.Text>
    </Space> : <Typography.Text type="secondary">
      {preview?.status === "stale" ? "计划已修改，请重新生成预览。" : preview?.error || "默认只生成方案；需要动态拼接效果时可生成预览。"}
    </Typography.Text>}
  </Card>;
}
