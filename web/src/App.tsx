import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, App as AntApp, Button, Card, Checkbox, Col, Empty, Form, Input, InputNumber, Layout, Modal, Select,
  Popconfirm, Row, Space, Spin, Table, Tabs, Tag, Typography,
} from "antd";
import {
  ArrowLeftOutlined, FileTextOutlined, PauseOutlined, PlayCircleOutlined,
  PlusOutlined, SoundOutlined, StopOutlined,
} from "@ant-design/icons";
import {
  api, EventRec, RunListItem, RunMeta, STAGE_LABELS, STATUS_LABELS,
} from "./api";
import { StagePanels } from "./panels";
import { TimelineWorkspace } from "./timeline-workspace";
import { Comparison } from "./compare";
import { PreviewPlayer, PreviewInfo } from "./preview";
import { replaceRunLocation } from "./route";
import "./workflow.css";

const { Header, Content } = Layout;

type CompareView = { type: "compare"; ids: string[]; backTo?: string; backStage?: string; backPane?: string };
type RunViewRoute = { type: "run"; id: string; compareBack?: CompareView; stage?: string; pane?: string };
type View = { type: "list" } | RunViewRoute | CompareView;

const RESUMABLE = ["failed", "canceled", "paused", "interrupted"];
const TERMINAL = ["done", ...RESUMABLE];
const STAGE_PURPOSE: Record<string, string> = {
  scan_media: "核对素材清单、缩略图、画幅和时长，确认输入可用。",
  plan_segments: "将文案拆成旁白段落，查看情绪角色、强度和结构弧线。",
  understand_media: "逐视频识别镜头、代表帧和运动变化，供后续选镜。",
  build_timeline: "看分镜并调整镜头、顺序和时长；自检结果随计划重建。",
  explore_web: "查看网络补充素材及验证状态，处理待人工补充的画面。",
  pick_music: "查看主配乐与备选，试听已下载的音轨。",
  write_doc: "检查粗剪方案与动态预览，导出可交接的计划文件。",
  finishing_guide: "按同一时间码查看转场、调色、音乐和交付检查，在达芬奇等软件中执行精剪。",
};
const WORKFLOW_PHASES = [
  { title: "准备素材与脚本", stages: ["scan_media", "plan_segments", "understand_media"] },
  { title: "搭建粗剪", stages: ["build_timeline", "explore_web", "pick_music"] },
  { title: "检查粗剪", stages: ["write_doc"] },
  { title: "交接精剪", stages: ["finishing_guide"] },
] as const;
const STAGE_STATUS_LABELS: Record<string, string> = {
  pending: "待开始", running: "进行中", paused: "已暂停", done: "已完成",
  degraded: "降级完成", failed: "失败", skipped: "已跳过",
};
const EVENT_STATUS_LABELS: Record<string, string> = {
  start: "阶段开始", "pause-requested": "收到暂停请求，正在等待当前子任务结束",
  "run-paused": "任务已暂停", "run-canceled": "任务已取消",
  "run-done": "全流程已完成", failed: "阶段失败",
  "recovered-interrupted": "检测到中断，可从断点恢复",
  "recovered-paused": "已恢复暂停状态",
};

const validRunId = (id: string | null): id is string => !!id && /^[A-Za-z0-9_-]+$/.test(id);

function viewFromLocation(): View {
  const params = new URLSearchParams(window.location.search);
  const run = params.get("run");
  const compare = params.getAll("compare");
  const ids = compare.length === 2 && compare.every((id) => validRunId(id)) && compare[0] !== compare[1]
    ? compare : undefined;
  const backTo = params.get("backTo");
  const returnTo = validRunId(backTo) ? backTo : undefined;
  const validStage = (value: string | null) => value && Object.prototype.hasOwnProperty.call(STAGE_PURPOSE, value)
    ? value : undefined;
  const validPane = (value: string | null) => value && ["storyboard", "editor", "critique"].includes(value)
    ? value : undefined;
  const backStage = validStage(params.get("returnStage"));
  const backPane = validPane(params.get("returnPane"));
  if (validRunId(run)) {
    return { type: "run", id: run,
      compareBack: ids ? { type: "compare", ids, backTo: returnTo, backStage, backPane } : undefined,
      stage: validStage(params.get("stage")), pane: validPane(params.get("pane")) };
  }
  if (ids) return { type: "compare", ids, backTo: returnTo, backStage, backPane };
  return { type: "list" };
}

function locationForView(view: View): string {
  const url = new URL(window.location.href);
  const params = new URLSearchParams();
  if (view.type === "run") {
    params.set("run", view.id);
    if (view.stage) params.set("stage", view.stage);
    if (view.pane) params.set("pane", view.pane);
    for (const id of view.compareBack?.ids ?? []) params.append("compare", id);
    if (view.compareBack?.backTo) params.set("backTo", view.compareBack.backTo);
    if (view.compareBack?.backStage) params.set("returnStage", view.compareBack.backStage);
    if (view.compareBack?.backPane) params.set("returnPane", view.compareBack.backPane);
  } else if (view.type === "compare") {
    for (const id of view.ids) params.append("compare", id);
    if (view.backTo) params.set("backTo", view.backTo);
    if (view.backStage) params.set("returnStage", view.backStage);
    if (view.backPane) params.set("returnPane", view.backPane);
  }
  url.search = params.toString();
  return url.toString();
}

export default function App() {
  const [view, setView] = useState<View>(viewFromLocation);
  const [showNew, setShowNew] = useState(false);
  useEffect(() => {
    const onHistory = () => setView(viewFromLocation());
    window.addEventListener("popstate", onHistory);
    return () => window.removeEventListener("popstate", onHistory);
  }, []);
  const navigate = (next: View) => {
    window.history.pushState(null, "", locationForView(next));
    setView(next);
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ background: "#10151f", display: "flex", alignItems: "center", gap: 16, padding: "0 24px" }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0 }}>
          ✂ Cut Agent <Typography.Text type="secondary" style={{ fontSize: 14 }}>粗剪控制台</Typography.Text>
        </Typography.Title>
        <div style={{ flex: 1 }} />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowNew(true)}>
          新建粗剪任务
        </Button>
      </Header>
      <Content style={{ padding: 20 }}>
        {view.type === "list" ? (
          <RunList onOpen={(id) => navigate({ type: "run", id })} onCompare={(ids) => navigate({ type: "compare", ids })} />
        ) : view.type === "compare" ? (
          <Space direction="vertical" style={{ width: "100%" }}><Button onClick={() => navigate(view.backTo
            ? { type: "run", id: view.backTo, stage: view.backStage, pane: view.backPane }
            : { type: "list" })}>{view.backTo ? "返回任务" : "返回列表"}</Button>
            <Comparison ids={view.ids} onOpen={(id) => navigate({ type: "run", id,
              compareBack: { type: "compare", ids: view.ids, backTo: view.backTo,
                backStage: view.backStage, backPane: view.backPane } })} /></Space>
        ) : (
          <RunView key={view.id} id={view.id} backLabel={view.compareBack ? "返回对照" : undefined}
            initialStage={view.stage} initialPane={view.pane}
            onBack={() => navigate(view.compareBack ?? { type: "list" })}
            onCompare={(ids) => {
              const current = viewFromLocation();
              navigate({ type: "compare", ids, backTo: view.id,
                backStage: current.type === "run" ? current.stage : undefined,
                backPane: current.type === "run" ? current.pane : undefined });
            }} />
        )}
      </Content>
      <NewRunModal
        open={showNew}
        onClose={() => setShowNew(false)}
        onCreated={(id) => { setShowNew(false); navigate({ type: "run", id }); }}
      />
    </Layout>
  );
}

// ---------------- 轮询 hook ----------------

function usePolling(active: boolean, ms: number, fn: () => void) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!active) return;
    fnRef.current();
    const t = setInterval(() => fnRef.current(), ms);
    return () => clearInterval(t);
  }, [active, ms]);
}

// ---------------- run 列表 ----------------

function RunList({ onOpen, onCompare }: { onOpen: (id: string) => void; onCompare: (ids: string[]) => void }) {
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const { message: msgApi } = AntApp.useApp();

  usePolling(true, 3000, useCallback(async () => {
    try { setRuns(await api.listRuns()); } catch { /* 服务未就绪 */ }
  }, []));

  const doResume = async (id: string) => {
    try { await api.resume(id); onOpen(id); } catch (e) { msgApi.error(String(e)); }
  };

  const variant = async (id: string) => {
    try {
      const response = await fetch(`/api/runs/${id}/variant`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail);
      onOpen(data.run_id);
    } catch (error) { msgApi.error(String(error)); }
  };

  const columns = [
    {
      title: "任务",
      dataIndex: "id",
      render: (id: string, r: RunListItem) => (
        <Space direction="vertical" size={0}>
          <Typography.Text code>{id}</Typography.Text>
          <Typography.Text type="secondary">seed {r.seed ?? "未固定"}{r.comparison_group !== id ? ` · 同组 ${r.comparison_group}` : ""}</Typography.Text>
          <Typography.Text type="secondary" ellipsis style={{ maxWidth: 320, display: "inline-block" }}>
            {r.copy_head || "（无文案预览）"}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 130,
      render: (s: string) => (
        <Tag color={s === "done" ? "green" : s === "running" ? "blue" : s === "failed" ? "red" : s === "paused" || s === "interrupted" ? "orange" : "default"}>
          {STATUS_LABELS[s] ?? s}
        </Tag>
      ),
    },
    {
      title: "进度",
      width: 110,
      render: (_: unknown, r: RunListItem) => `${r.stages_done}/${r.stages_total} 阶段`,
    },
    {
      title: "断点",
      dataIndex: "resumable_from",
      width: 130,
      render: (s?: string | null) => (s ? <Tag>{STAGE_LABELS[s] ?? s}</Tag> : <Typography.Text type="secondary">-</Typography.Text>),
    },
    {
      title: "操作",
      width: 170,
      render: (_: unknown, r: RunListItem) => (
        <Space>
          <Button size="small" onClick={() => onOpen(r.id)}>查看</Button>
          {r.status === "done" && <Button size="small" onClick={() => void variant(r.id)}>另一版</Button>}
          {RESUMABLE.includes(r.status) && (
            <Button size="small" type="primary" ghost icon={<PlayCircleOutlined />} onClick={() => doResume(r.id)}>
              恢复
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Table
      title={() => <Space><Button disabled={selected.length !== 2} onClick={() => onCompare(selected.map(String))}>A/B 并排对照</Button><Typography.Text type="secondary">选择两项已完成任务；另一版默认 seed 加一。</Typography.Text></Space>}
      rowSelection={{ selectedRowKeys: selected, onChange: setSelected, getCheckboxProps: (r) => ({ disabled: r.status !== "done" }) }}
      rowKey="id"
      size="middle"
      columns={columns}
      dataSource={runs}
      scroll={{ x: 960 }}
      pagination={{ pageSize: 15 }}
      locale={{ emptyText: <Empty description="还没有任务，点右上角「新建粗剪任务」" /> }}
    />
  );
}

// ---------------- run 详情 ----------------

function RunView({ id, onBack, onCompare, initialStage, initialPane, backLabel = "返回" }: {
  id: string; onBack: () => void; onCompare: (ids: string[]) => void;
  initialStage?: string; initialPane?: string; backLabel?: string;
}) {
  const [meta, setMeta] = useState<RunMeta | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [relatedRuns, setRelatedRuns] = useState<RunListItem[]>([]);
  const [comparisonTarget, setComparisonTarget] = useState<string>();
  const [events, setEvents] = useState<EventRec[]>([]);
  const [arts, setArts] = useState<Record<string, unknown>>({});
  const [tabKey, setTabKey] = useState<string | null>(initialStage ?? null);
  const [timelineEditRequest, setTimelineEditRequest] = useState<{ seq: number; token: number }>();
  const timelineEditToken = useRef(0);
  const [showMobileStages, setShowMobileStages] = useState(false);
  const phaseNavRef = useRef<HTMLElement>(null);
  const sinceRef = useRef(0);
  const fetchedRef = useRef<Map<string, string>>(new Map());
  const versionsRef = useRef<Record<string, string>>({});
  const metaRequestRef = useRef(0);
  const metaSettledRef = useRef(0);
  const { message: msgApi } = AntApp.useApp();

  const loadMeta = useCallback(async () => {
    const request = ++metaRequestRef.current;
    try {
      const m = await api.run(id);
      if (request < metaSettledRef.current) return;
      metaSettledRef.current = request;
      setMeta(m);
      setSyncError(null);
      versionsRef.current = m.artifact_versions ?? {};
      for (const name of fetchedRef.current.keys()) {
        if (!m.has_artifacts?.[name]) fetchedRef.current.delete(name);
      }
      setArts((previous) => Object.fromEntries(
        Object.entries(previous).filter(([name]) => m.has_artifacts?.[name])));
      for (const [name, has] of Object.entries(m.has_artifacts ?? {})) {
        const version = m.artifact_versions?.[name] ?? "";
        if (has && fetchedRef.current.get(name) !== version) {
          fetchedRef.current.set(name, version);
          api.stage(id, name)
            .then((a) => {
              if (versionsRef.current[name] === version) setArts((p) => ({ ...p, [name]: a }));
            })
            .catch(() => {
              if (fetchedRef.current.get(name) === version) fetchedRef.current.delete(name);
            });
        }
      }
    } catch (error) {
      if (request < metaSettledRef.current) return;
      metaSettledRef.current = request;
      setSyncError(String(error));
    }
  }, [id]);

  const loadEvents = useCallback(async () => {
    try {
      const r = await api.events(id, sinceRef.current);
      // StrictMode and a poll tick can overlap. Compare against the cursor at
      // response time so two requests for the same range cannot append twice.
      const fresh = r.events.filter((event) => event.seq > sinceRef.current);
      if (fresh.length) {
        sinceRef.current = fresh[fresh.length - 1].seq;
        setEvents((prev) => [...prev, ...fresh].slice(-400));
      }
    } catch { /* ignore */ }
  }, [id]);

  const terminal = meta ? TERMINAL.includes(meta.status) : false;
  usePolling(true, terminal ? 8000 : 1500, loadMeta);
  usePolling(true, terminal ? 8000 : 1500, loadEvents);
  usePolling(meta?.status === "done", 8000, useCallback(async () => {
    try {
      const listed = await api.listRuns();
      const current = listed.find((run) => run.id === id);
      const group = current?.comparison_group ?? current?.id;
      const related = group ? listed.filter((run) => run.id !== id && run.status === "done"
        && (run.comparison_group ?? run.id) === group) : [];
      if (group && group !== id) related.sort((a, b) => Number(b.id === group) - Number(a.id === group));
      setRelatedRuns(related);
    } catch { /* 保留已找到的同组任务 */ }
  }, [id]));

  // 阶段进度（供 Steps 与 Tab 跟随；meta 为空时用安全值）
  const runningIdx = meta ? meta.stages.findIndex((s) => s.status === "running" || s.status === "paused") : -1;
  const doneCount = meta ? meta.stages.filter((s) => s.status === "done" || s.status === "degraded").length : 0;
  const currentIdx = runningIdx >= 0
    ? runningIdx
    : Math.min(doneCount, Math.max(0, (meta?.stages.length ?? 1) - 1));
  const currentStageName = meta?.stages[currentIdx]?.name ?? "";
  const hasTimeline = !!((arts.build_timeline as { timeline?: unknown[] } | undefined)?.timeline?.length);
  const selectedStageName = tabKey ?? (meta?.status === "done" && hasTimeline ? "build_timeline" : currentStageName);
  const phaseNavReady = !!meta && !meta.export_pending;
  useEffect(() => {
    const nav = phaseNavRef.current;
    if (!phaseNavReady || !nav || !selectedStageName || window.innerWidth <= 850) return;
    const node = Array.from(nav.querySelectorAll<HTMLButtonElement>(".run-stage-node"))
      .find((item) => item.dataset.stage === selectedStageName);
    if (!node) return;
    const navRect = nav.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    if (nodeRect.bottom > navRect.bottom - 8) nav.scrollTop += nodeRect.bottom - navRect.bottom + 8;
    else if (nodeRect.top < navRect.top + 8) nav.scrollTop -= navRect.top - nodeRect.top + 8;
  }, [selectedStageName, phaseNavReady]);
  const doAction = async (kind: "pause" | "cancel" | "resume") => {
    try {
      await api[kind](id);
      msgApi.success(kind === "pause" ? "暂停请求已发出（当前子任务结束后生效）"
        : kind === "cancel" ? "取消请求已发出" : "已恢复运行");
      setTimeout(loadMeta, 400);
    } catch (e) {
      msgApi.error(String(e));
    }
  };

  if (!meta) {
    return (
      <Space direction="vertical" style={{ width: "100%" }}>
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>{backLabel}</Button>
        {syncError ? <Alert type="warning" showIcon message="任务状态暂时无法同步"
          description={<Space wrap>正在自动重试。{syncError}<Button size="small" onClick={() => void loadMeta()}>立即重试</Button></Space>} />
          : <Spin />}
      </Space>
    );
  }

  if (meta.export_pending) {
    return <Space direction="vertical" style={{ width: "100%" }}>
      <Button icon={<ArrowLeftOutlined />} onClick={onBack}>{backLabel}</Button>
      <Typography.Title level={4}>正在保存或恢复编辑结果</Typography.Title>
      <Typography.Paragraph>{meta.error}</Typography.Paragraph>
      <Typography.Text type="secondary">保存完成后会自动显示方案，也可刷新重试。</Typography.Text>
      <Button onClick={() => void loadMeta()}>刷新</Button>
    </Space>;
  }

  const docArt = arts.write_doc as { doc_path?: string; preview?: PreviewInfo | null; revision?: number } | undefined;
  const musicArt = arts.pick_music as { music?: { downloads?: { local: string }[] } } | undefined;
  const defaultStage = meta.status === "done" && hasTimeline ? "build_timeline" : currentStageName;
  const activeStage = meta.stages[currentIdx];
  const viewedStage = meta.stages.find((stage) => stage.name === selectedStageName);
  const compareWith = relatedRuns.some((run) => run.id === comparisonTarget)
    ? comparisonTarget : relatedRuns[0]?.id;
  const chooseStage = (name: string | null) => {
    const selected = name && name !== defaultStage ? name : null;
    setTabKey(selected);
    replaceRunLocation("stage", selected ?? undefined);
  };
  const jumpToStage = (name: string) => {
    chooseStage(name);
    requestAnimationFrame(() =>
      document.querySelector(".run-stage-tabs")?.scrollIntoView({ behavior: "auto", block: "start" }));
  };
  const editTimelineRow = (seq: number) => {
    setTimelineEditRequest({ seq, token: ++timelineEditToken.current });
    jumpToStage("build_timeline");
  };
  const selectStage = (name: string) => chooseStage(name);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {syncError && <Alert type="warning" showIcon message="连接中断，正在自动重试"
        description={<Space wrap>当前显示上次同步的阶段与产物，请在恢复连接后继续编辑。<Button size="small"
          onClick={() => void loadMeta()}>立即重试</Button></Space>} />}
      <Row align="middle" gutter={[12, 8]} style={{ width: "100%" }}>
        <Col>
          <Button icon={<ArrowLeftOutlined />} onClick={onBack}>{backLabel}</Button>
        </Col>
        <Col flex="1 1 220px" style={{ minWidth: 0 }}>
          <Typography.Text code copyable style={{ fontSize: 15, overflowWrap: "anywhere" }}>{meta.id}</Typography.Text>
        </Col>
        <Col>
          <Tag color={meta.status === "done" ? "green" : meta.status === "running" ? "blue" : meta.status === "failed" ? "red" : "orange"}>
            {STATUS_LABELS[meta.status] ?? meta.status}
          </Tag>
        </Col>
        {meta.seed != null && <Tag>seed {meta.seed}</Tag>}
        {meta.resumable_from && RESUMABLE.includes(meta.status) && (
          <Tag color="orange">断点：{STAGE_LABELS[meta.resumable_from] ?? meta.resumable_from}</Tag>
        )}
        <Col flex="none" style={{ marginLeft: "auto", maxWidth: "100%" }}>
          <Space wrap>
            {meta.status === "running" && (
              <>
                <Button icon={<PauseOutlined />} onClick={() => doAction("pause")}>暂停</Button>
                <Popconfirm title="取消后保留已产出的阶段，可恢复。确定？" onConfirm={() => doAction("cancel")}>
                  <Button danger icon={<StopOutlined />}>取消</Button>
                </Popconfirm>
              </>
            )}
            {RESUMABLE.includes(meta.status) && (
              <Button type="primary" icon={<PlayCircleOutlined />} onClick={() => doAction("resume")}>
                恢复
              </Button>
            )}
            {docArt?.doc_path && (
              <a href={api.fileUrl(docArt.doc_path)}>
                <Button icon={<FileTextOutlined />}>粗剪方案.md</Button>
              </a>
            )}
            {meta.status === "done" && compareWith && <>
              {relatedRuns.length > 1 && <Select aria-label="选择对比版本" value={compareWith}
                onChange={setComparisonTarget} style={{ minWidth: 220 }}
                options={relatedRuns.map((run) => ({ value: run.id,
                  label: `seed ${run.seed ?? "未固定"} · ${run.id}` }))} />}
              <Button onClick={() => onCompare([id, compareWith])}>对比另一版</Button>
            </>}
            {musicArt?.music?.downloads?.[0]?.local && (
              <a href={api.fileUrl(musicArt.music.downloads[0].local)} download>
                <Button icon={<SoundOutlined />}>BGM 下载</Button>
              </a>
            )}
          </Space>
        </Col>
      </Row>

      {meta.error && <Typography.Text type="warning">⚠ {meta.error}</Typography.Text>}
      {meta.status === "running" && meta.control_requested && (
        <Typography.Text type="warning">
          {meta.control_requested === "cancel" ? "取消" : "暂停"}请求已收到，当前子任务结束后生效。
        </Typography.Text>
      )}
      <Card size="small" className="run-current-stage" title={meta.status === "done" ? `流程已完成：${meta.stages.length}/${meta.stages.length} 阶段`
        : `当前流程节点：第 ${currentIdx + 1}/${meta.stages.length} 阶段 · ${STAGE_LABELS[activeStage.name] ?? activeStage.name}`}
        extra={tabKey && tabKey !== defaultStage ? <Button size="small" onClick={() => chooseStage(null)}>
          {meta.status === "done" ? "回到时间线编排" : "回到当前阶段"}
        </Button> : undefined}>
        <Typography.Text type="secondary">{meta.status === "done"
          ? "可按阶段回看产物；从「时间线编排」调整方案，在「文档产出」检查预览，最后将「精剪指导」交给剪辑软件执行。"
          : STAGE_PURPOSE[activeStage.name]}</Typography.Text>
        {meta.status !== "done" && activeStage.progress && <Typography.Paragraph style={{ margin: "8px 0 0" }}>
          进度 {activeStage.progress.done}/{activeStage.progress.total}
          {activeStage.progress.item ? ` · 正在处理 ${activeStage.progress.item}` : ""}
        </Typography.Paragraph>}
        {activeStage.status === "running" && !activeStage.progress && <Typography.Paragraph style={{ margin: "8px 0 0" }}>正在处理本阶段…</Typography.Paragraph>}
        {viewedStage && (meta.status === "done" || viewedStage.name !== activeStage.name) &&
          <Typography.Paragraph className="run-viewed-stage">
            当前查看：{STAGE_LABELS[viewedStage.name] ?? viewedStage.name} · {STAGE_STATUS_LABELS[viewedStage.status] ?? viewedStage.status}
          </Typography.Paragraph>}
      </Card>
      <div className="run-workspace">
      <section ref={phaseNavRef} className={`run-phase-overview${showMobileStages ? " is-mobile-open" : ""}`} aria-label="粗剪流程阶段">
        <div className="run-phase-overview__intro">
          <Typography.Text strong>粗剪流程 · 4 个阶段 / {meta.stages.length} 个节点</Typography.Text>
          <Typography.Text type="secondary">选择节点查看产物；高亮边框标出当前流程节点。</Typography.Text>
        </div>
        {WORKFLOW_PHASES.map((phase, phaseIndex) => {
          const nodes = phase.stages.map((name) => meta.stages.find((stage) => stage.name === name)).filter((stage): stage is NonNullable<typeof stage> => !!stage);
          const finished = nodes.filter((stage) => stage.status === "done" || stage.status === "degraded").length;
          const phaseActive = nodes.some((stage) => stage.name === activeStage.name) && meta.status !== "done";
          return <div className={`run-phase${phaseActive ? " is-active" : ""}`} key={phase.title}>
            <div className="run-phase__heading">
              <span className="run-phase__number">{phaseIndex + 1}</span>
              <strong>{phase.title}</strong>
              <span className="run-phase__count">{finished}/{nodes.length}</span>
            </div>
            <div className="run-phase__nodes">
              {nodes.map((stage) => <button type="button" key={stage.name}
                className={`run-stage-node${selectedStageName === stage.name ? " is-selected" : ""}${activeStage.name === stage.name && meta.status !== "done" ? " is-current" : ""}`}
                data-stage={stage.name} aria-pressed={selectedStageName === stage.name}
                onClick={() => selectStage(stage.name)}>
                <span className="run-stage-node__top"><strong>{STAGE_LABELS[stage.name] ?? stage.name}</strong>
                  <span className={`run-stage-node__status status-${stage.status}`}>{STAGE_STATUS_LABELS[stage.status] ?? stage.status}</span></span>
                <span className="run-stage-node__purpose">{STAGE_PURPOSE[stage.name]}</span>
                {stage.progress && (stage.status === "running" || stage.status === "paused") &&
                  <span className="run-stage-node__progress">{stage.progress.done}/{stage.progress.total}
                    {stage.progress.item ? ` · ${stage.progress.item}` : ""}</span>}
              </button>)}
            </div>
          </div>;
        })}
      </section>
      <div className="run-stage-picker">
        <div className="run-stage-picker__heading">
          <Typography.Text type="secondary">查看阶段</Typography.Text>
          <Button size="small" aria-expanded={showMobileStages} onClick={() => setShowMobileStages((value) => !value)}>
            {showMobileStages ? "收起完整流程" : "查看完整流程"}
          </Button>
        </div>
        <Select aria-label="查看阶段" value={selectedStageName} onChange={selectStage}
          options={meta.stages.map((stage) => ({ value: stage.name,
            label: `${STAGE_LABELS[stage.name] ?? stage.name} · ${STAGE_STATUS_LABELS[stage.status] ?? stage.status}` }))}
          style={{ width: "100%" }} />
      </div>
      <Tabs
        className="run-stage-tabs"
        type="card"
        activeKey={selectedStageName}
        onChange={selectStage}
        items={meta.stages.map((s) => {
          const name = s.name;
          const has = !!arts[name];
          const webRows = name === "explore_web"
            ? (arts[name] as { timeline?: { source?: string; needs_web?: boolean }[] } | undefined)?.timeline
            : undefined;
          const webWarningsResolved = !!webRows && !webRows.some((row) => row.source === "web" && row.needs_web !== false);
          return {
            key: name,
            label: (
              <span>
                {STAGE_LABELS[name] ?? name}
                {s.status === "running" && <Spin size="small" style={{ marginLeft: 6 }} />}
              </span>
            ),
            children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Card size="small" title={`${STAGE_LABELS[name] ?? name} · ${STAGE_STATUS_LABELS[s.status] ?? s.status}`}>
                <Typography.Text type="secondary">{STAGE_PURPOSE[name]}</Typography.Text>
                {s.progress && <Typography.Paragraph style={{ margin: "8px 0 0" }}>
                  已处理 {s.progress.done}/{s.progress.total}{s.progress.item ? ` · ${s.progress.item}` : ""}
                </Typography.Paragraph>}
                {!!s.warnings?.length && <Typography.Paragraph type="warning" style={{ margin: "8px 0 0" }}>
                  {webWarningsResolved ? "执行时记录（当前时间线已处理）：" : ""}{s.warnings.join("；")}
                </Typography.Paragraph>}
                {meta.status === "done" && name === "write_doc" && <Space wrap style={{ marginTop: 8 }}>
                  <Button size="small" onClick={() => jumpToStage("build_timeline")}>返回时间线调整</Button>
                  {meta.has_artifacts?.finishing_guide && <Button size="small" type="primary"
                    onClick={() => jumpToStage("finishing_guide")}>下一步：精剪指导</Button>}
                </Space>}
                {meta.status === "done" && name === "finishing_guide" && <Button size="small"
                  style={{ marginTop: 8 }} onClick={() => jumpToStage("write_doc")}>返回预览</Button>}
              </Card>
              {name === "build_timeline" && hasTimeline ? <TimelineWorkspace arts={arts} runId={id}
                editable={meta.status === "done" && !!docArt} revision={docArt?.revision}
                editRequest={timelineEditRequest} initialPane={initialPane}
                onChanged={() => { fetchedRef.current.clear(); void loadMeta(); }}
                onOpenPreview={() => jumpToStage("write_doc")} />
                : name === "write_doc" && has ? <>
                {meta.status === "done" && docArt && <PreviewPlayer runId={id} preview={docArt.preview} revision={docArt.revision}
                  onChanged={() => { fetchedRef.current.clear(); void loadMeta(); }}
                  onEditRow={hasTimeline ? editTimelineRow : undefined} />}
                <StagePanels runId={id} stage={name} art={arts[name] as Record<string, unknown>} mediaDir={meta.media_dir}
                  version={meta.artifact_versions?.[name]} arts={arts} />
              </> : has ? <StagePanels runId={id} stage={name} art={arts[name] as Record<string, unknown>}
                mediaDir={meta.media_dir} version={meta.artifact_versions?.[name]} arts={arts}
                onEditRow={meta.status === "done" && hasTimeline ? editTimelineRow : undefined} />
                : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={meta.has_artifacts?.[name] ? "正在加载阶段产物…"
                    : s.status === "running" ? "正在处理…" : s.status === "paused" ? "已暂停在该阶段" : "该阶段尚未开始"} />}
              <EventLog events={events.filter((event) => event.stage === name)} title="本阶段事件" />
            </Space>,
          };
        })}
      />
      </div>
    </Space>
  );
}

function EventLog({ events, title = "事件流" }: { events: EventRec[]; title?: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [events.length]);
  return (
    <div style={{ background: "#0d1119", borderRadius: 8, padding: 12, height: 260, overflowY: "auto" }} ref={boxRef}>
      <Typography.Text strong>{title}</Typography.Text>
      {events.length === 0 && <div style={{ color: "#555", marginTop: 8 }}>（暂无事件）</div>}
      {events.map((e) => (
        <div key={e.seq} style={{ fontFamily: "Consolas, monospace", fontSize: 12, marginTop: 3, color: e.type === "status" ? "#7ec3ff" : "#b8c2d0" }}>
          <span style={{ color: "#555" }}>{new Date(e.ts * 1000).toLocaleTimeString()}</span>{" "}
          <span style={{ color: "#8a7ff0" }}>[{STAGE_LABELS[e.stage] ?? e.stage}]</span>{" "}
          {e.type === "status" ? (EVENT_STATUS_LABELS[String(e.what ?? "")] ?? String(e.what ?? ""))
            : e.type === "progress" ? `${e.done}/${e.total} ${e.item ?? ""}` : String(e.msg ?? "")}
        </div>
      ))}
    </div>
  );
}

// ---------------- 新建任务 ----------------

function NewRunModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const { message: msgApi } = AntApp.useApp();

  const submit = async () => {
    const v = await form.validateFields();
    setBusy(true);
    try {
      const r = await api.createRun(v.media_dir, v.copy, v.seed ?? undefined, !!v.preview, v.platform);
      msgApi.success(`粗剪任务已启动：${r.run_id}`);
      form.resetFields();
      onCreated(r.run_id);
    } catch (e) {
      msgApi.error(`启动失败: ${e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="新建粗剪任务"
      open={open}
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="启动"
      cancelText="取消"
      width={560}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item name="platform" label="交付建议预设" initialValue="douyin">
          <Select options={[{ value: "douyin", label: "抖音 · 竖屏" }, { value: "xiaohongshu", label: "小红书 · 3:4" }, { value: "bilibili", label: "B站 · 横屏" }]} />
        </Form.Item>
        <Form.Item name="preview" valuePropName="checked" initialValue={false}>
          <Checkbox>同时生成带段号的粗剪预览</Checkbox>
        </Form.Item>
        <Form.Item name="media_dir" label="素材目录（绝对路径，含视频/图片）"
          rules={[{ required: true, message: "请输入素材目录" }]}
          initialValue="E:/Code/Cut_Agent/sample_media">
          <Input placeholder="E:/Code/Cut_Agent/sample_media" />
        </Form.Item>
        <Form.Item name="copy" label="文案" rules={[{ required: true, message: "请输入文案" }]}>
          <Input.TextArea rows={5} placeholder="粘贴或输入视频文案…" />
        </Form.Item>
        <Form.Item name="seed" label="随机种子（可选；A/B 对照时固定同一值）">
          <InputNumber style={{ width: "100%" }} placeholder="留空=不固定" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
