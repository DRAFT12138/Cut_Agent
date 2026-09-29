import { useEffect, useRef, useState } from "react";
import { Badge, Button, Empty, Tabs, Typography } from "antd";
import { CritiquePanel } from "./panels";
import { PlanEditor } from "./editor";
import { Storyboard } from "./storyboard";
import { replaceRunLocation } from "./route";

type Pane = "storyboard" | "editor" | "critique";
type Critique = Parameters<typeof CritiquePanel>[0]["report"];

export function TimelineWorkspace({ arts, runId, editable, revision, editRequest, initialPane, onChanged, onOpenPreview }: {
  arts: Record<string, unknown>;
  runId: string;
  editable: boolean;
  revision?: number;
  editRequest?: { seq: number; token: number };
  initialPane?: string;
  onChanged: () => void;
  onOpenPreview: () => void;
}) {
  const [pane, setPane] = useState<Pane>(initialPane === "editor" || initialPane === "critique" ? initialPane : "storyboard");
  const choosePane = (next: Pane) => {
    setPane(next);
    replaceRunLocation("pane", next === "storyboard" ? undefined : next);
  };
  const [focusRow, setFocusRow] = useState<{ seq: number; token: number }>();
  const focusToken = useRef(0);
  useEffect(() => {
    if (!editable || !editRequest) return;
    focusToken.current = Math.max(focusToken.current, editRequest.token);
    setFocusRow(editRequest);
    choosePane("editor");
  }, [editable, editRequest?.token]);
  const report = (arts.write_doc as { critique?: Critique } | undefined)?.critique ??
    (arts.explore_web as { critique?: Critique } | undefined)?.critique ??
    (arts.build_timeline as { critique?: Critique } | undefined)?.critique;
  const warningCount = report?.warnings?.length ?? 0;
  return <div className="timeline-workspace">
    <Typography.Paragraph type="secondary">先看分镜，再调整镜头、顺序和时长；自检随每次保存更新。</Typography.Paragraph>
    <Tabs activeKey={pane} onChange={(key) => choosePane(key as Pane)} items={[
      { key: "storyboard", label: "1 · 看分镜", children:
        <Storyboard arts={arts} runId={runId} onEditRow={editable ? (seq) => {
          setFocusRow({ seq, token: ++focusToken.current });
          choosePane("editor");
        } : undefined} /> },
      { key: "editor", label: "2 · 调整镜头", disabled: !editable,
        children: editable ? <>
          <PlanEditor runId={runId} publishedRevision={revision}
            focusRow={focusRow} onChanged={onChanged} />
          <Button style={{ marginTop: 12 }} onClick={() => choosePane("critique")}>下一步：查看自检</Button>
        </> : <Empty description="文档产出后可编辑" /> },
      { key: "critique", label: <span>3 · 查看自检 {warningCount > 0 && <Badge count={warningCount} size="small" />}</span>,
        children: <>
          <CritiquePanel report={report} />
          {editable && <Button type="primary" style={{ marginTop: 12 }} onClick={onOpenPreview}>下一步：检查预览</Button>}
        </> },
    ]} />
  </div>;
}
