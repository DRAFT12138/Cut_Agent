import { useEffect, useState } from "react";
import { Alert, Button, Card, Col, Row, Space, Spin, Table, Tag, Typography } from "antd";
import { api } from "./api";
import { Storyboard } from "./storyboard";
import { CritiquePanel } from "./panels";

type TimelineRow = { seq: number; media?: string; shot_idx?: number | null; start_offset?: number;
  use_duration: number; segment_text?: string };
type ComparePlan = { copy?: string; media_folder?: string; platform?: string;
  timeline: TimelineRow[]; media?: unknown[]; doc_path?: string; storyboard?: unknown;
  revision?: number; preview?: { status: string }; critique?: Parameters<typeof CritiquePanel>[0]["report"] };
type CompareMeta = { id: string; seed?: number | null;
  config?: { options?: { comparison_group?: string; parent_run?: string } } };
type CompareRecord = { plan: ComparePlan; meta: CompareMeta };
type Difference = { seq: number; a?: TimelineRow; b?: TimelineRow; changes: string[] };

function differences(a: ComparePlan, b: ComparePlan): Difference[] {
  const rows: Difference[] = [];
  for (let i = 0; i < Math.max(a.timeline.length, b.timeline.length); i++) {
    const left = a.timeline[i], right = b.timeline[i];
    const changes: string[] = [];
    if (!left || !right) changes.push(left ? "仅 A 有此行" : "仅 B 有此行");
    else {
      if (left.media !== right.media || left.shot_idx !== right.shot_idx) changes.push("镜头");
      if (Math.abs((left.start_offset ?? 0) - (right.start_offset ?? 0)) > .001) changes.push("入点");
      if (Math.abs(left.use_duration - right.use_duration) > .001) changes.push("时长");
      if ((left.segment_text ?? "") !== (right.segment_text ?? "")) changes.push("旁白");
    }
    if (changes.length) rows.push({ seq: i + 1, a: left, b: right, changes });
  }
  return rows;
}

function clipLabel(row?: TimelineRow): string {
  if (!row) return "—";
  return `${row.media || "待补素材"}${row.shot_idx != null ? ` #${row.shot_idx}` : ""} · ${row.use_duration.toFixed(2)}s`;
}

export function Comparison({ ids, onOpen }: { ids: string[]; onOpen: (id: string) => void }) {
  const [records, setRecords] = useState<CompareRecord[]>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setRecords(undefined);
    setError("");
    Promise.all(ids.map(async (id) => {
      const [planResponse, metaResponse] = await Promise.all([
        fetch(`/api/runs/${id}/plan`), fetch(`/api/runs/${id}`),
      ]);
      if (!planResponse.ok) throw new Error(`${id} 的计划尚未生成，请待任务完成后重试`);
      if (!metaResponse.ok) throw new Error(`${id} 的任务信息暂不可用，请稍后重试`);
      return { plan: await planResponse.json() as ComparePlan,
        meta: await metaResponse.json() as CompareMeta };
    })).then((data) => { if (active) setRecords(data); })
      .catch((reason) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [ids]);

  if (error) return <Alert type="warning" message={error} />;
  if (!records || records.length !== 2) return <Spin />;
  const [a, b] = records.map((record) => record.plan);
  const [aMeta, bMeta] = records.map((record) => record.meta);
  const changed = differences(a, b);
  const sameInput = a.copy === b.copy && a.media_folder === b.media_folder;
  const groupOf = (meta: CompareMeta) => meta.config?.options?.comparison_group ?? meta.id;
  const sameGroup = groupOf(aMeta) === groupOf(bMeta);
  const total = (plan: ComparePlan) => plan.timeline.reduce((sum, row) => sum + row.use_duration, 0);
  const delta = total(b) - total(a);
  return <div>
    <Card size="small" title="两版差异" style={{ marginBottom: 16 }}>
      <Typography.Paragraph style={{ marginBottom: 8 }}>
        <Tag color={sameInput ? "green" : "orange"}>{sameInput ? "文案与素材目录相同" : "输入不同，请谨慎比较"}</Tag>
        <Tag color={sameGroup ? "green" : "default"}>{sameGroup ? "同一 A/B 组" : "独立任务对照"}</Tag>
        <Tag>seed A {aMeta.seed ?? "未固定"} / B {bMeta.seed ?? "未固定"}</Tag>
        <Tag>{changed.length} 行有差异</Tag>
        <Tag color={Math.abs(delta) > .001 ? "blue" : "default"}>B 比 A {delta >= 0 ? "长" : "短"} {Math.abs(delta).toFixed(2)}s</Tag>
      </Typography.Paragraph>
      {changed.length ? <Table size="small" rowKey="seq" pagination={false} scroll={{ x: 620 }}
        dataSource={changed} columns={[
          { title: "行", dataIndex: "seq", width: 55, render: (seq: number) => `#${seq}` },
          { title: "A", dataIndex: "a", render: clipLabel },
          { title: "B", dataIndex: "b", render: clipLabel },
          { title: "变化", dataIndex: "changes", width: 190,
            render: (items: string[]) => items.map((item) => <Tag key={item} color="blue">{item}</Tag>) },
        ]} /> : <Typography.Text type="secondary">镜头、入点、时长和旁白均相同；可继续比较预览和自检建议。</Typography.Text>}
    </Card>
    <Row gutter={[16, 16]}>{ids.map((id, index) => <Col xs={24} xl={12} key={id}>
      <ComparisonPlan id={id} label={index === 0 ? "A" : "B"} plan={records[index].plan}
        changedRows={changed.map((row) => row.seq)} onOpen={() => onOpen(id)} />
    </Col>)}</Row>
  </div>;
}

function ComparisonPlan({ id, label, plan, changedRows, onOpen }: {
  id: string; label: string; plan: ComparePlan; changedRows: number[]; onOpen: () => void;
}) {
  return <Card title={`${label} · ${id}`}>
    <Space wrap style={{ marginBottom: 12 }}>
      <Button type="primary" onClick={onOpen}>打开 {label} 版继续</Button>
      <a href={api.handoffUrl(id)} download="精剪交接包.zip"><Button>下载 {label} 版交接包</Button></a>
    </Space>
    <Typography.Paragraph>总时长 {plan.timeline.reduce((sum, row) => sum + row.use_duration, 0).toFixed(2)}s · {plan.timeline.length} 行</Typography.Paragraph>
    <Typography.Paragraph ellipsis={{ rows: 2, expandable: true }}>{plan.copy}</Typography.Paragraph>
    {plan.preview?.status === "ready" && <video controls preload="metadata" aria-label={`${label} 版粗剪预览`}
      src={`/api/runs/${id}/preview?revision=${plan.revision ?? 0}`}
      style={{ width: "100%", maxHeight: 300, background: "#000", marginBottom: 12 }} />}
    <Storyboard runId={id} highlightRows={changedRows} arts={{ build_timeline: plan, understand_media: plan,
      write_doc: { doc_path: plan.doc_path, storyboard: plan.storyboard, revision: plan.revision } }} />
    <CritiquePanel report={plan.critique} />
  </Card>;
}
