// 分镜条（storyboard）：时间线 × 镜头代表帧，Phase 0 的主视图

import { Button, Col, Empty, Row, Space, Tag, Typography } from "antd";
import { api, fmtDur } from "./api";

interface Shot {
  idx: number;
  start: number;
  end: number;
  motion?: number;
  frames?: string[];
  frame_times?: number[];
}

interface MediaRow {
  name: string;
  kind: string;
  duration?: number;
  shots?: Shot[];
  description?: string;
  thumbnails?: string[];
}

interface TLRow {
  seq: number;
  media: string;
  kind: string;
  use_duration: number;
  start_offset?: number;
  segment_text?: string;
  source?: string;
  note?: string;
  needs_web?: boolean;
  shot_idx?: number | null;
  shot_end_idx?: number;
  span?: boolean;
  shot_description?: string;
  role?: string;
  intensity?: number;
  asset_status?: string;
  thumbnail?: string;
}

interface ExecutionCard {
  seq: number;
  frame: string;
  frame_missing: boolean;
  source_in_tc: string;
  source_out_tc: string;
  timeline_in_tc: string;
  timeline_out_tc: string;
  source_fps: number;
  source_fps_assumed: boolean;
  source_timing_note?: string;
  shortfall: number;
}

/** Before document export, show a seconds estimate; exported cards supply frame timecodes. */
export function fmtTC(sec: number): string {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}

/** 为时间线行挑代表帧：取 [off, off+dur] 区间内最近的镜头代表帧。 */
function pickFrame(m: MediaRow | undefined, off: number, dur: number): { src: string; shotIdx?: number } | null {
  if (!m?.shots?.length) return null;
  const target = off + dur / 2;
  let best: { src: string; shotIdx?: number; d: number } | null = null;
  for (const s of m.shots) {
    for (let i = 0; i < (s.frames?.length ?? 0); i++) {
      const t = s.frame_times?.[i];
      if (t == null || t < off || t >= off + dur) continue;
      const d = Math.abs(t - target);
      if (!best || d < best.d) best = { src: s.frames![i], shotIdx: s.idx, d };
    }
  }
  return best ? { src: best.src, shotIdx: best.shotIdx } : null;
}

export function Storyboard({
  arts,
  runId,
  onEditRow,
  highlightRows,
}: {
  arts: Record<string, unknown>;
  runId: string;
  onEditRow?: (seq: number) => void;
  highlightRows?: number[];
}) {
  const tl = ((arts.write_doc as { timeline?: TLRow[] } | undefined)?.timeline ??
    (arts.explore_web as { timeline?: TLRow[] } | undefined)?.timeline ??
    (arts.build_timeline as { timeline?: TLRow[] } | undefined)?.timeline ?? []) as TLRow[];
  const media = ((arts.understand_media as { media?: MediaRow[] } | undefined)?.media ??
    (arts.scan_media as { media?: MediaRow[] } | undefined)?.media ?? []) as MediaRow[];
  const mIndex: Record<string, MediaRow> = {};
  const document = arts.write_doc as { revision?: number; doc_path?: string; storyboard?: { cards: ExecutionCard[] } } | undefined;
  for (const m of media) mIndex[m.name] = m;

  if (tl.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="（暂无时间线）" />;
  }

  let t = 0;

  return (
    <Row gutter={[10, 10]}>
      {tl.map((row) => {
        const off = row.kind === "video" ? (row.start_offset ?? 0) : 0;
        const dur = row.use_duration ?? 0;
        const start = t;
        t += dur;
        const m = mIndex[row.media];
        const frame = row.source === "web"
          ? row.asset_status === "verified" && row.thumbnail ? { src: row.thumbnail } : null
          : m?.kind === "image" && m.thumbnails?.[0] ? { src: m.thumbnails[0] } : pickFrame(m, off, dur);
        const card = document?.storyboard?.cards.find((c) => c.seq === row.seq);
        const portableFrame = card && document?.doc_path
          ? document.doc_path.replace(/[^/\\]+$/, "") + card.frame : undefined;
        const isWeb = !m || row.source === "web";
        return (
          <Col xs={12} sm={8} md={6} lg={4} xl={4} key={row.seq}>
            <div
              style={{
                background: "#10151f",
                borderRadius: 8,
                overflow: "hidden",
                border: highlightRows?.includes(row.seq) ? "2px solid #77b7ff" : "1px solid #1d2534",
              }}
            >
              <div style={{ position: "relative", background: "#000", aspectRatio: "16/9" }}>
                {portableFrame || frame ? (
                  <img
                    src={`${api.fileUrl(portableFrame ?? frame!.src, runId)}&revision=${document?.revision ?? 0}`}
                    alt={row.media}
                    style={{ width: "100%", height: "100%", objectFit: "cover" }}
                  />
                ) : (
                  <div
                    style={{
                      width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center",
                      color: isWeb ? "#f0a060" : "#555", fontSize: 13,
                    }}
                  >
                    {isWeb ? "⚠ 待补素材" : "（无代表帧）"}
                  </div>
                )}
                <Tag color={isWeb ? row.asset_status === "verified" ? "green" : "orange" : "blue"}
                  style={{ position: "absolute", top: 4, left: 4, margin: 0 }}>
                  #{row.seq}
                </Tag>
                {highlightRows?.includes(row.seq) && <Tag color="blue" style={{ position: "absolute", top: 4, right: 4, margin: 0 }}>有差异</Tag>}
                <Tag style={{ position: "absolute", bottom: 4, right: 4, margin: 0, background: "rgba(0,0,0,.7)" }}>
                  {card ? `${card.timeline_in_tc}–${card.timeline_out_tc}` : `${fmtTC(start)}–${fmtTC(start + dur)}`}
                </Tag>
              </div>
              <div style={{ padding: "6px 8px" }}>
                <Space size={2} wrap style={{ marginBottom: 4 }}>
                  {row.role && <Tag color={row.role === "高潮" ? "magenta" : "geekblue"}>{row.role}</Tag>}
                  {row.intensity != null && <Tag>强度 {row.intensity}/5</Tag>}
                  {isWeb && <Tag color={row.asset_status === "verified" ? "green" : "orange"}>
                    {row.asset_status === "verified" ? "网络已入库" : "网络待补"}
                  </Tag>}
                </Space>
                <Typography.Text ellipsis style={{ display: "block", fontSize: 13, maxWidth: 150 }}
                  title={`${row.media}（入点 ${off.toFixed(1)}s，${fmtDur(dur)}）`}>
                  {row.media || "（待补）"}
                </Typography.Text>
                <div style={{ fontSize: 12, color: "#8a93a3", marginTop: 2 }}>
                  {row.kind === "video" ? (
                    <span>
                      入 {card?.source_in_tc ?? fmtTC(off)} · {fmtDur(dur)}
                      {(row.shot_idx ?? frame?.shotIdx) != null && <Tag color="geekblue" style={{ marginLeft: 6 }}>镜头{row.shot_idx ?? frame?.shotIdx}{row.span ? `–${row.shot_end_idx}` : ""}</Tag>}
                    </span>
                  ) : (
                    <span>图片 · {fmtDur(dur)}</span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: "#b8c2d0", marginTop: 3, lineHeight: 1.4 }}>
                  {row.segment_text ? `“${row.segment_text}”` : "无旁白画面"}
                </div>
                {row.note && (
                  <div style={{ fontSize: 11, color: "#f0a060", marginTop: 2 }}>⚠ {row.note}</div>
                )}
                {card && <div style={{ fontSize: 11, marginTop: 4 }}>
                  出 {card.source_out_tc}（不含） · {card.source_fps} fps{card.source_fps_assumed ? "（估算）" : ""}
                  <div>{card.source_timing_note}</div>
                  {card.shortfall > .05 && <div style={{ color: "#f0a060" }}>画面短于旁白 {card.shortfall.toFixed(2)}s</div>}
                  {card.frame_missing && <div style={{ color: "#f0a060" }}>代表帧待补充</div>}
                </div>}
                {onEditRow && <Button size="small" block style={{ marginTop: 8 }} onClick={() => onEditRow(row.seq)}>
                  调整此行
                </Button>}
              </div>
            </div>
          </Col>
        );
      })}
      <Col span={24}>
        <Space>
          <Typography.Text type="secondary">
            总时长 {fmtTC(t)}（{tl.length} 行）
          </Typography.Text>
        </Space>
      </Col>
    </Row>
  );
}
