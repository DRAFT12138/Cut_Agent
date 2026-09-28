import { useCallback, useEffect, useState } from "react";
import { App, Button, Card, InputNumber, Select, Space, Tag, Typography } from "antd";
import { api, fmtDur } from "./api";
import "./editor.css";

type Shot = { idx: number; frames?: string[] };
type Media = { name: string; kind?: string; thumbnails?: string[]; shots?: Shot[] };
type Row = { seq: number; media: string; kind?: string; source?: string; thumbnail?: string;
  start_offset?: number; use_duration: number; segment_text?: string; role?: string;
  shot_idx?: number | null; alternatives?: { media: string; shot_idx: number; reason: string }[] };
type Plan = { revision?: number; doc_path?: string;
  storyboard?: { cards?: { seq: number; frame: string }[] };
  timeline: Row[]; media: Media[] };

function selectedFrame(plan: Plan, row: Row): string | undefined {
  const card = plan.storyboard?.cards?.find((item) => item.seq === row.seq);
  if (card && plan.doc_path) return plan.doc_path.replace(/[^/\\]+$/, "") + card.frame;
  if (row.source === "web") return row.thumbnail;
  const asset = plan.media.find((item) => item.name === row.media);
  if (asset?.kind === "image") return asset.thumbnails?.[0];
  const shot = asset?.shots?.find((item) => item.idx === row.shot_idx);
  return shot?.frames?.[0] ?? asset?.shots?.flatMap((item) => item.frames ?? [])[0] ?? asset?.thumbnails?.[0];
}

function alternativeFrame(plan: Plan, mediaName: string, shotIdx: number): string | undefined {
  const asset = plan.media.find((item) => item.name === mediaName);
  return asset?.shots?.find((shot) => shot.idx === shotIdx)?.frames?.[0] ?? asset?.thumbnails?.[0];
}

export function PlanEditor({ runId, publishedRevision, focusRow, onChanged }: {
  runId: string; publishedRevision?: number; focusRow?: { seq: number; token: number };
  onChanged: () => void;
}) {
  const [plan, setPlan] = useState<Plan>();
  const [busy, setBusy] = useState(false);
  const [dragged, setDragged] = useState<number>();
  const [dropTarget, setDropTarget] = useState<number>();
  const [append, setAppend] = useState<string>();
  const { message } = App.useApp();
  const load = useCallback(async () => {
    const response = await fetch(`/api/runs/${runId}/plan`);
    if (!response.ok) throw new Error("计划尚不可编辑");
    const incoming = await response.json() as Plan;
    setPlan((current) => !current || (incoming.revision ?? 0) >= (current.revision ?? 0) ? incoming : current);
    return incoming;
  }, [runId]);
  useEffect(() => { load().catch((e) => message.error(String(e))); }, [load, message]);
  useEffect(() => {
    if (busy || !plan || publishedRevision == null || publishedRevision <= (plan.revision ?? 0)) return;
    load().then((incoming) => {
      if ((incoming.revision ?? 0) > (plan.revision ?? 0)) message.info(`计划已在其他页面更新至修订 ${incoming.revision}`);
    }).catch((e) => message.error(String(e)));
  }, [busy, load, message, plan?.revision, publishedRevision]);
  const hasPlan = !!plan;
  useEffect(() => {
    if (!hasPlan || !focusRow) return;
    const frame = requestAnimationFrame(() =>
      document.getElementById(`edit-row-${focusRow.seq}`)?.scrollIntoView({ behavior: "auto", block: "center" }));
    return () => cancelAnimationFrame(frame);
  }, [hasPlan, focusRow?.token]);

  const edit = async (operation: Record<string, unknown>) => {
    if (!plan || busy) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/runs/${runId}/edit`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...operation, expected_revision: plan.revision ?? 0 }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "保存失败");
      setPlan(result);
      onChanged();
      message.success("计划已保存并重建");
    } catch (error) {
      message.error(String(error));
      await load();
    } finally { setBusy(false); }
  };

  if (!plan) return null;
  const options = plan.media.flatMap((m) => m.shots?.length
    ? m.shots.map((s) => ({ label: `${m.name} #${s.idx}`, value: JSON.stringify({ media: m.name, shot: s.idx }) }))
    : [{ label: m.name, value: JSON.stringify({ media: m.name }) }]);
  const imageUrl = (path?: string) => path ? `${api.fileUrl(path, runId)}&revision=${plan.revision ?? 0}` : undefined;

  return <Card title="调整计划" size="small" id="plan-editor">
    <Typography.Paragraph type="secondary">看图换镜；拖动或指定目标行调整顺序，也可用上移/下移。目标行不在屏幕内时，可边拖边滚动页面，或直接选目标行。保存后重建文档与自检，旁白随行移动。</Typography.Paragraph>
    <div className="plan-editor__rows">
      {plan.timeline.map((row, index) => <div key={`${plan.revision}-${row.seq}`} id={`edit-row-${row.seq}`}
        className={`plan-editor__row${dropTarget === row.seq ? " is-drop-target" : ""}`}
        onDragOver={(event) => { if (dragged != null && dragged !== row.seq) { event.preventDefault(); setDropTarget(row.seq); } }}
        onDragLeave={() => setDropTarget(undefined)}
        onDrop={(event) => {
          event.preventDefault();
          if (dragged != null && dragged !== row.seq) void edit({ op: "move", row: dragged, to: row.seq });
          setDragged(undefined); setDropTarget(undefined);
        }}>
        <div className="plan-editor__current">
          {selectedFrame(plan, row) ? <img src={imageUrl(selectedFrame(plan, row))} alt={`第 ${row.seq} 行当前画面`} />
            : <div className="plan-editor__placeholder">画面待补</div>}
          <div className="plan-editor__current-text">
            <Space size={6} wrap><Typography.Text strong>#{row.seq} · {row.media || "待补素材"}</Typography.Text>
              {row.role && <Tag>{row.role}</Tag>}
              <Typography.Text type="secondary">{fmtDur(row.use_duration)}{row.kind === "video" ? ` · 入点 ${(row.start_offset ?? 0).toFixed(2)}s` : ""}</Typography.Text>
            </Space>
            <Typography.Paragraph style={{ margin: "6px 0" }}>{row.segment_text || "无旁白画面"}</Typography.Paragraph>
            <span className="plan-editor__drag-handle" draggable={!busy}
              onDragStart={(event) => { event.dataTransfer.effectAllowed = "move"; setDragged(row.seq); }}
              onDragEnd={() => { setDragged(undefined); setDropTarget(undefined); }}
              title="拖到目标行；较远的行可边拖边滚动页面">⋮⋮ 拖动排序</span>
          </div>
        </div>
        {!!row.alternatives?.length && <div className="plan-editor__alternatives" aria-label={`第 ${row.seq} 行备选镜头`}>
          {row.alternatives.map((candidate) => <button type="button" className="plan-editor__candidate"
            key={`${candidate.media}-${candidate.shot_idx}`} disabled={busy}
            onClick={() => void edit({ op: "swap", row: row.seq, media: candidate.media, shot: candidate.shot_idx })}
            title={candidate.reason}>
            {alternativeFrame(plan, candidate.media, candidate.shot_idx)
              ? <img src={imageUrl(alternativeFrame(plan, candidate.media, candidate.shot_idx))}
                  alt={`${candidate.media} 镜头 ${candidate.shot_idx} 备选画面`} />
              : <span className="plan-editor__candidate-placeholder">无代表帧</span>}
            <span className="plan-editor__candidate-copy">
              <strong>换入 {candidate.media} #{candidate.shot_idx}</strong>
              <small>{candidate.reason}</small>
            </span>
          </button>)}
        </div>}
        <Space wrap>
          <Select placeholder="选择其他素材/镜头" style={{ width: 240 }} options={options}
            disabled={busy} onChange={(value) => void edit({ op: "swap", row: row.seq, ...JSON.parse(value) })} />
          <InputNumber aria-label={`第 ${row.seq} 行秒数`} min={0.04} step={0.1} defaultValue={row.use_duration}
            disabled={busy} onBlur={(event) => {
              const seconds = Number(event.target.value);
              if (Number.isFinite(seconds) && seconds > 0 && seconds !== row.use_duration)
                void edit({ op: "duration", row: row.seq, seconds });
            }} addonAfter="秒" />
          {plan.timeline.length > 2 && <Select aria-label={`第 ${row.seq} 行目标位置`}
            placeholder="移动到第…行" style={{ width: 145 }} disabled={busy}
            options={plan.timeline.filter((target) => target.seq !== row.seq)
              .map((target) => ({ label: `第 ${target.seq} 行`, value: target.seq }))}
            onChange={(to) => void edit({ op: "move", row: row.seq, to })} />}
          <Button disabled={busy || index === 0} onClick={() => void edit({ op: "move", row: row.seq, to: row.seq - 1 })}>上移</Button>
          <Button disabled={busy || index === plan.timeline.length - 1} onClick={() => void edit({ op: "move", row: row.seq, to: row.seq + 1 })}>下移</Button>
          <Button danger disabled={busy} onClick={() => void edit({ op: "drop", row: row.seq })}>删除行</Button>
        </Space>
      </div>)}
    </div>
    <Space wrap style={{ marginTop: 12 }}>
      <Select placeholder="选择追加镜头" options={options} style={{ width: 280 }} value={append} onChange={setAppend} />
      <Button disabled={busy || !append} onClick={() => append && void edit({ op: "append", ...JSON.parse(append) })}>追加</Button>
      <Button disabled={busy} onClick={() => void load()}>刷新计划</Button>
    </Space>
  </Card>;
}
