// 各阶段面板（run 详情页 Tabs 内容）

import { useEffect, useState } from "react";
import { App, Button, Card, Checkbox, Col, Empty, Image, Row, Space, Table, Tag, Typography } from "antd";
import { api, fmtDur, ReviewState, SourceCheck } from "./api";
import { DocumentViewer } from "./document-viewer";
import { Storyboard } from "./storyboard";

export interface ShotInfo {
  idx: number;
  start: number;
  end: number;
  motion: number;
  cut_score?: number;
  frame_times?: number[];
  frame_motions?: number[];
  frames?: string[];
  description?: string;
  roles?: string[];
}

export interface MediaRow {
  name: string;
  kind: string;
  duration: number;
  width: number;
  height: number;
  size_mb?: number;
  shots?: ShotInfo[];
  description?: string;
  scene_cuts?: number[];
  thumbnails?: string[];
  sampling?: { detected_shots: number; covered_shots: number; missing_shots: number[] };
}

function FrameImg({ src, run, alt = "素材缩略图" }: { src: string; run: string; alt?: string }) {
  if (!src) return null;
  return (
    <Image
      src={api.fileUrl(src, run)}
      alt={alt}
      width={110}
      style={{ borderRadius: 4, objectFit: "cover" }}
      preview={{ mask: null }}
    />
  );
}

// ---------------- 扫描素材 ----------------

function ScanPanel({ art, run, mediaDir }: { art: any; run: string; mediaDir?: string }) {
  const media: MediaRow[] = art.media ?? [];
  return (
    <Table
      size="small"
      rowKey="name"
      dataSource={media}
      pagination={false}
      columns={[
        {
          title: "缩略图",
          width: 100,
          render: (_: unknown, m: MediaRow) =>
            m.thumbnails?.length ? (
              <div style={{ display: "flex", gap: 6, maxWidth: 250, overflowX: "auto" }}>
                {m.thumbnails.map((path, i) => <FrameImg key={path} src={path} run={run} alt={`${m.name} · 缩略图 ${i + 1}`} />)}
              </div>
            ) : m.kind === "image" && mediaDir ? (
              <FrameImg src={`${mediaDir.replace(/\\/g, "/")}/${m.name}`} run={run} alt={m.name} />
            ) : (
              <Typography.Text type="secondary">缩略图暂不可用</Typography.Text>
            ),
        },
        { title: "文件", dataIndex: "name" },
        { title: "类型", dataIndex: "kind", width: 70 },
        { title: "时长", width: 90, render: (_: unknown, m: MediaRow) => fmtDur(m.duration) },
        { title: "分辨率", width: 110, render: (_: unknown, m: MediaRow) => `${m.width}x${m.height}` },
        { title: "场景切点", width: 90, render: (_: unknown, m: MediaRow) => (m.scene_cuts?.length ?? 0) },
        { title: "大小", width: 80, render: (_: unknown, m: MediaRow) => `${(m.size_mb ?? 0).toFixed(1)} MB` },
      ]}
    />
  );
}

// ---------------- 文案分段 ----------------

function IntensityArc({ values, title }: { values: number[]; title: string }) {
  if (!values.length) return null;
  const x = (i: number) => values.length === 1 ? 170 : 30 + i * 280 / (values.length - 1);
  const y = (value: number) => 78 - Math.max(1, Math.min(5, value)) * 12;
  return <div>
    <Typography.Paragraph style={{ marginBottom: 4 }}>{title}：{values.join(" → ")}</Typography.Paragraph>
    <svg width="340" height="104" viewBox="0 0 340 104" style={{ maxWidth: "100%" }}
      role="img" aria-label={`${title}，强度 1 至 5：${values.join("、")}`}>
      {[1, 3, 5].map((level) => <g key={level}>
        <line x1="26" x2="326" y1={y(level)} y2={y(level)} stroke="#293447" />
        <text x="10" y={y(level) + 4} fill="#8a93a3" fontSize="11">{level}</text>
      </g>)}
      <polyline fill="none" stroke="#3ba7ff" strokeWidth="2"
        points={values.map((value, i) => `${x(i)},${y(value)}`).join(" ")} />
      {values.map((value, i) => <g key={i}>
        <circle cx={x(i)} cy={y(value)} r="3" fill="#3ba7ff"><title>第 {i + 1} 段：{value}/5</title></circle>
        <text x={x(i)} y="94" textAnchor="middle" fill="#8a93a3" fontSize="10">#{i + 1}</text>
      </g>)}
    </svg>
  </div>;
}

function PlanPanel({ art }: { art: any }) {
  const segs: { text: string; mood?: string; duration?: number; role?: string; intensity?: number; role_source?: string }[] = art.segments ?? [];
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <IntensityArc values={segs.map((s) => s.intensity ?? 3)} title="文案分段强度（初稿）" />
      <Row gutter={[12, 12]}>
      {segs.map((s, i) => (
        <Col xs={24} md={12} key={i}>
          <Card size="small" title={`#${i + 1}`}>
            <Typography.Paragraph style={{ margin: 0 }}>{s.text}</Typography.Paragraph>
            <Space size={4} wrap style={{ marginTop: 8 }}>
              {s.mood && <Tag color="purple">{s.mood}</Tag>}
              {s.role && <Tag color="blue">{s.role}{s.role_source === "rule" ? "（规则）" : ""}</Tag>}
              {s.intensity != null && <Tag>强度 {s.intensity}/5</Tag>}
              {s.duration != null && <Tag>{fmtDur(s.duration)}</Tag>}
            </Space>
          </Card>
        </Col>
      ))}
      </Row>
    </Space>
  );
}

export function CritiquePanel({ report }: { report?: {
  repairs?: { message: string }[];
  warnings?: { seq?: number; message: string }[];
  intensity_arc?: number[];
  disclosure?: string;
} }) {
  if (!report) return null;
  const arc = report.intensity_arc ?? [];
  return <Card size="small" title="剪辑自检">
    <IntensityArc values={arc} title="当前时间线强度（自检后）" />
    <Typography.Paragraph type="secondary">按当前时间线行序展示；自动修复或手动移行后，曲线顺序可能与文案分段初稿不同。</Typography.Paragraph>
    <Typography.Paragraph type="secondary">{report.disclosure}</Typography.Paragraph>
    {report.repairs?.map((r, i) => <Typography.Paragraph key={`repair-${i}`}>已修复：{r.message}</Typography.Paragraph>)}
    {report.warnings?.map((w, i) => <Typography.Paragraph type="warning" key={`warning-${i}`}>
      待人工：{w.seq ? `第 ${w.seq} 行：` : ""}{w.message}
    </Typography.Paragraph>)}
    {!report.warnings?.length && <Typography.Text>本轮规则未发现待处理告警，仍需人工审核语义。</Typography.Text>}
  </Card>;
}

// ---------------- 视觉理解 ----------------

function UnderstandPanel({ art, run }: { art: any; run: string }) {
  const media: MediaRow[] = art.media ?? [];
  const videos = media.filter((m) => m.kind === "video");
  return videos.length === 0 ? (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有视频素材（或视觉层关闭）" />
  ) : (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {videos.map((m) => (
        <Card key={m.name} size="small" title={<span>{m.name} <Tag>{fmtDur(m.duration)}</Tag></span>}>
          {m.sampling?.detected_shots != null && <Typography.Paragraph type={m.sampling.missing_shots?.length ? "warning" : "secondary"}>
            代表帧覆盖 {m.sampling.covered_shots}/{m.sampling.detected_shots} 个镜头
            {(m.sampling.missing_shots?.length ?? 0) > 0 && `；缺少镜头 ${m.sampling.missing_shots.join("、")}`}
          </Typography.Paragraph>}
          {m.description && (
            <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
              {m.description}
            </Typography.Text>
          )}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            运动条按本视频最活跃镜头归一化，数值为原始运动度。
          </Typography.Paragraph>
          <div style={{ display: "flex", gap: 8, overflowX: "auto", paddingBottom: 4 }}>
            {(m.shots ?? []).map((s: ShotInfo) => (
              <div key={s.idx} style={{ flexShrink: 0, width: 290, padding: 8, background: "#101723", borderRadius: 6 }}>
                <div style={{ display: "flex", gap: 6, overflowX: "auto" }}>
                  {s.frames?.length ? s.frames.map((path, i) => <div key={path} style={{ flexShrink: 0 }}>
                    <FrameImg src={path} run={run} alt={`${m.name} #${s.idx} · ${s.frame_times?.[i]?.toFixed(2) ?? "?"}s`} />
                    <div style={{ fontSize: 10, color: "#8a93a3" }}>
                      {s.frame_times?.[i]?.toFixed(2) ?? "?"}s
                      {s.frame_motions?.[i] != null && ` · 运动度 ${s.frame_motions[i].toFixed(1)}`}
                    </div>
                  </div>)
                    : <Typography.Text type="secondary">代表帧暂不可用</Typography.Text>}
                </div>
                <div style={{ fontSize: 11, marginTop: 2, color: "#8a93a3" }}>
                  #{s.idx} {s.start.toFixed(1)}–{s.end.toFixed(1)}s
                </div>
                <div style={{ fontSize: 11, color: s.motion > 8 ? "#f0a060" : "#6b7280" }}>
                  motion {s.motion?.toFixed(1)}
                </div>
                <div role="meter" aria-label={`镜头 ${s.idx} 运动度`} aria-valuemin={0}
                  aria-valuemax={Math.max(1, ...(m.shots ?? []).map((shot) => shot.motion || 0))}
                  aria-valuenow={s.motion || 0} style={{ height: 5, background: "#253247", borderRadius: 3, margin: "6px 0 10px" }}>
                  <div style={{ height: "100%", borderRadius: 3, background: "#3ba7ff",
                    width: `${Math.max(0, s.motion || 0) / Math.max(1, ...(m.shots ?? []).map((shot) => shot.motion || 0)) * 100}%` }} />
                </div>
                <div style={{ fontSize: 12, whiteSpace: "normal" }}>
                  {s.description || "镜头描述暂不可用"}
                </div>
                {s.roles?.map((role) => <Tag key={role}>{role}</Tag>)}
              </div>
            ))}
            {(m.shots ?? []).length === 0 && (
              <Typography.Text type="secondary">（无镜头信息）</Typography.Text>
            )}
          </div>
        </Card>
      ))}
    </Space>
  );
}

// ---------------- 网络素材 ----------------

function WebPanel({ art, run, onEditRow }: { art: any; run: string; onEditRow?: (seq: number) => void }) {
  const items: { name: string; url: string; query?: string; from?: string; error?: string; for_segment?: string; for_seq?: number;
    status?: string; local?: string; thumbnail?: string; width?: number; height?: number; kind?: string }[] =
    art.web_assets ?? [];
  const rows: { seq: number; source?: string; needs_web?: boolean; segment_text?: string }[] = art.timeline ?? [];
  const pendingCount = rows.filter((row) => row.source === "web" && row.needs_web !== false).length;
  const needsManual = pendingCount > 0;
  return items.length === 0 ? (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={needsManual ? "搜索未返回可用素材，请手动补充时间线中的待补画面" : "本地素材已覆盖，无网络素材"} />
  ) : (
    <>
    <Typography.Paragraph type={needsManual ? "warning" : "secondary"}>
      当前时间线待替换 {pendingCount} 行。{!needsManual && items.some((item) => item.status !== "verified")
        ? "原链接失败记录保留供追溯；对应画面已在时间线处理。" : "可在下方查看每条素材的验证结果。"}
    </Typography.Paragraph>
    <Row gutter={[12, 12]}>
      {items.map((w, i) => {
        const target = rows.find((row) =>
          (w.for_seq != null ? row.seq === w.for_seq : row.segment_text === w.for_segment));
        const needsReplacement = target?.source === "web" && target.needs_web !== false;
        return (
        <Col key={`${w.url}-${i}`} xs={24} md={12} xl={8}>
          <Card size="small" title={w.name || `网络素材 ${i + 1}`} style={{ height: "100%" }}>
          <Space direction="vertical" size={0} style={{ width: "100%" }}>
            {w.status === "verified" && w.thumbnail ? <Image src={api.fileUrl(w.thumbnail, run)} alt={w.name}
              width="100%" height={160} style={{ objectFit: "contain", background: "#0b0e14", borderRadius: 4 }} />
              : <div style={{ padding: "24px 0", color: "#8a93a3" }}>暂无已验证缩略图</div>}
            <Space wrap>
              {w.status === "verified" ? (
                <Tag color="green">已验证入库</Tag>
              ) : <Tag color={needsReplacement ? "orange" : "default"}>
                {needsReplacement ? "待人工替换" : target || w.for_seq === 0 ? "原链接不可用" : "待人工"}
              </Tag>}
              {w.status !== "verified" && target && !needsReplacement && <Tag color="green">第 {target.seq} 行已处理</Tag>}
              {w.query && <Typography.Text type="secondary">搜索: {w.query}</Typography.Text>}
            </Space>
            {w.width && w.height && <Typography.Text type="secondary">{w.kind === "video" ? "视频" : "图片"} · {w.width}×{w.height}</Typography.Text>}
            {w.for_segment && <Typography.Paragraph style={{ margin: "8px 0" }}>对应旁白：{w.for_segment}</Typography.Paragraph>}
            {w.url && (
              <a href={w.url} target="_blank" rel="noreferrer">
                {w.from || "查看来源"}
              </a>
            )}
            {w.local && <a href={api.fileUrl(w.local, run)} target="_blank" rel="noreferrer">打开本地素材</a>}
            {w.error && <Typography.Text type="danger" style={{ fontSize: 12 }}>{w.error}</Typography.Text>}
            {w.status !== "verified" && needsReplacement && target && (onEditRow
              ? <Button size="small" type="primary" onClick={() => onEditRow(target.seq)}>去替换第 {target.seq} 行</Button>
              : <Typography.Text type="secondary">任务完成后可在时间线替换此画面</Typography.Text>)}
          </Space>
          </Card>
        </Col>
      ); })}
    </Row>
    </>
  );
}

// ---------------- 配乐 ----------------

function MusicPanel({ art, run }: { art: any; run: string }) {
  const music = art.music ?? {};
  const p = music.primary ?? {};
  const alts: { title: string; artist?: string; reason?: string }[] = music.alternatives ?? [];
  const dl: { local: string; title?: string; url?: string; size_kb?: number }[] = music.downloads ?? [];
  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Card size="small">
        <Typography.Text strong>主 BGM：{p.title ?? "待定"}</Typography.Text>
        {p.artist && <Tag style={{ marginLeft: 8 }}>{p.artist}</Tag>}
        {p.reason && <div style={{ marginTop: 4, color: "#8a93a3", fontSize: 13 }}>{p.reason}</div>}
        {music.mood && <Tag color="purple" style={{ marginTop: 8 }}>情绪: {music.mood}</Tag>}
      </Card>
      {alts.length > 0 && (
        <div>
          {alts.map((a, i) => (
            <div key={i} style={{ fontSize: 13, color: "#8a93a3" }}>
              备选：{a.title} {a.artist ? `(${a.artist})` : ""} — {a.reason}
            </div>
          ))}
        </div>
      )}
      {dl.map((d, i) => (
        <Card key={i} size="small">
          <Space direction="vertical" size={2} style={{ width: "100%" }}>
            <Typography.Text>{d.title ?? "已下载 BGM"}</Typography.Text>
            <audio controls src={api.fileUrl(d.local, run)} style={{ width: "100%" }} />
          </Space>
        </Card>
      ))}
      {music.download_error && (
        <Typography.Text type="warning" style={{ fontSize: 13 }}>
          {music.download_error}
        </Typography.Text>
      )}
    </Space>
  );
}

// ---------------- 文档产出 ----------------

function DocPanel({ art, run, version }: { art: any; run: string; version: string }) {
  const docPath: string | undefined = art.doc_path;
  const linePath: string | undefined = art.line_doc_path;
  const planPath: string | undefined = art.plan_path;
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space wrap>
        {docPath && <a href={api.fileUrl(docPath, run)} download="plan.md"><Button>下载 plan.md</Button></a>}
        {planPath && <a href={api.fileUrl(planPath, run)} download="plan.json"><Button>下载 plan.json</Button></a>}
        {linePath && <a href={api.fileUrl(linePath, run)} download="cut_lines.txt"><Button>下载执行清单</Button></a>}
      </Space>
      {docPath ? <DocumentViewer path={docPath} run={run} version={version} />
        : <Empty description="文档尚未生成" />}
    </Space>
  );
}

// ---------------- 出口 ----------------

function FinishingPanel({ art, run, version, onEditRow }: { art: any; run: string; version: string;
  onEditRow?: (seq: number) => void }) {
  const [review, setReview] = useState<ReviewState | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceCheck | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [checkingSources, setCheckingSources] = useState(false);
  const [saving, setSaving] = useState(false);
  const { message } = App.useApp();
  useEffect(() => {
    let active = true;
    setReview(null);
    setReviewError(null);
    api.review(run).then((value) => { if (active) setReview(value); })
      .catch((error) => { if (active) setReviewError(String(error)); });
    return () => { active = false; };
  }, [run, version]);
  useEffect(() => {
    let active = true;
    setSources(null);
    setSourceError(null);
    setCheckingSources(true);
    api.sourceCheck(run).then((value) => { if (active) setSources(value); })
      .catch((error) => { if (active) setSourceError(String(error)); })
      .finally(() => { if (active) setCheckingSources(false); });
    return () => { active = false; };
  }, [run, version]);
  const refreshSources = async () => {
    setCheckingSources(true);
    setSources(null);
    setSourceError(null);
    try { setSources(await api.sourceCheck(run)); setSourceError(null); }
    catch (error) { setSourceError(String(error)); }
    finally { setCheckingSources(false); }
  };
  const updateCheck = async (index: number, checked: boolean) => {
    if (!review || saving) return;
    setReview({ ...review, items: review.items.map((item, i) => i === index ? { ...item, checked } : item) });
    setSaving(true);
    try {
      const item = review.items[index];
      setReview(await api.setReview(run, { expected_revision: review.plan_revision,
        index, text: item.text, checked }));
      setReviewError(null);
    } catch (error) {
      message.error(String(error));
      try { setReview(await api.review(run)); setReviewError(null); }
      catch (reloadError) { setReviewError(String(reloadError)); }
    } finally { setSaving(false); }
  };
  const guide = art.finishing;
  if (!guide) return <Empty description="精剪指导尚未生成" />;
  const directory = String(art.guide_path ?? "").replace(/[^/\\]+$/, "");
  const pictureIssues = sources?.pictures.filter((item) => item.availability !== "available"
    || (item.source === "local" && item.snapshot !== "match")) ?? [];
  const musicIssues = sources?.music.filter((item) => item.availability !== "available") ?? [];
  const availablePictures = sources?.pictures.filter((item) => item.availability === "available").length ?? 0;
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Space wrap>
      <a href={api.handoffUrl(run)} download="精剪交接包.zip"><Button type="primary">下载精剪交接包.zip</Button></a>
      <a href={api.sourcesUrl(run)} download="素材交接清单.md">下载素材交接清单.md</a>
      <a href={api.fileUrl(art.guide_path, run)} target="_blank" rel="noreferrer">导出原始精剪指导.md</a>
      <a href={api.reviewUrl(run)} download="精剪核对记录.md">下载人工核对记录.md</a>
    </Space>
    <Typography.Paragraph type="secondary">交接包含素材清单、指导、人工核对记录、粗剪方案、执行清单、计划及配图；方案引用的粗剪预览也会打包。按素材清单另行携带原始画面和配乐源文件，再到达芬奇等软件中执行精剪。</Typography.Paragraph>
    <Card size="small" title="交接前素材核对" extra={<Button size="small" loading={checkingSources}
      onClick={() => void refreshSources()}>重新核对</Button>}>
      {sourceError && <Typography.Paragraph type="danger">当前机器素材核对失败：{sourceError}</Typography.Paragraph>}
      {!sources && !sourceError && <Typography.Text type="secondary">正在核对当前机器上的源文件…</Typography.Text>}
      {sources && <>
        <Typography.Paragraph type={pictureIssues.length || musicIssues.length ? "warning" : "success"}>
          方案修订 {sources.revision}：画面文件可找到 {availablePictures}/{sources.pictures.length}；
          需复核画面 {pictureIssues.length} 行；已下载配乐缺失 {musicIssues.length} 份。
        </Typography.Paragraph>
        {pictureIssues.map((item) => <Typography.Paragraph key={item.seq} style={{ marginBottom: 6, overflowWrap: "anywhere" }}>
          <Tag color="warning">第 {item.seq} 行</Tag>{item.name}：{item.availability === "manual" ? "网络素材待人工替换"
            : item.availability === "missing" ? "原文件缺失"
              : item.snapshot === "changed" ? "与任务输入快照不同，需复核"
                : "任务输入未记录，需复核"}
          {onEditRow && <Button size="small" style={{ marginLeft: 8 }}
            onClick={() => onEditRow(item.seq)}>调整第 {item.seq} 行</Button>}
        </Typography.Paragraph>)}
        {musicIssues.map((item, index) => <Typography.Paragraph key={`${item.title}-${index}`} style={{ marginBottom: 6, overflowWrap: "anywhere" }}>
          <Tag color="warning">配乐</Tag>{item.title}：已下载源文件缺失
        </Typography.Paragraph>)}
        {!sources.music.length && <Typography.Paragraph type="secondary">当前方案没有已下载配乐源文件；按精剪指导另行准备。</Typography.Paragraph>}
        <Typography.Text type="secondary">本机核对仅比较本地画面的原始大小与修改时间；网络素材和配乐不在任务输入快照中。交接时仍需按清单另带源文件。</Typography.Text>
      </>}
    </Card>
    {guide.steps.map((step: any) => <Card size="small" key={step.seq}
      title={`#${step.seq} ${step.anchor.timeline_in_tc} → ${step.anchor.timeline_out_tc}`}>
      <Row gutter={[12, 8]} align="top">
        <Col flex="180px"><Image width={160} src={api.fileUrl(directory + step.anchor.frame, run)} /></Col>
        <Col flex="1 1 280px" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
          <Typography.Paragraph style={{ marginBottom: 6 }}><strong>取材：</strong>{step.anchor.media || "待补素材"}{step.anchor.shot_idx != null ? ` #${step.anchor.shot_idx}` : ""}</Typography.Paragraph>
          <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 6 }}><strong>旁白原文：</strong>{step.anchor.text || "无旁白画面"}</Typography.Paragraph>
        </Col>
      </Row>
      <Typography.Paragraph>{step.transition}</Typography.Paragraph>
      <Typography.Paragraph>{step.color} <Tag>{step.color_source === "model" ? "模型建议" : "规则建议"}</Tag></Typography.Paragraph>
      <Typography.Paragraph>{step.voice}</Typography.Paragraph>
      <Typography.Paragraph type="secondary">源 {step.anchor.source_in_tc} → {step.anchor.source_out_tc}（出点不含）<br />{step.anchor.source_timing_note}</Typography.Paragraph>
      <Typography.Text type={step.anchor.shortfall > .05 ? "warning" : "secondary"}>{step.rhythm}</Typography.Text>
      {onEditRow && <div style={{ marginTop: 8 }}><Button size="small"
        onClick={() => onEditRow(step.seq)}>调整第 {step.seq} 行</Button></div>}
    </Card>)}
    <Card size="small" title="音乐执行">
      <Typography.Paragraph>从帧 0 开始，淡入 {guide.music.fade_in_seconds}s，淡出 {guide.music.fade_out_seconds}s；旁白 ducking {guide.music.ducking_db}dB。</Typography.Paragraph>
      {guide.music.cues.map((cue: any) => <Typography.Paragraph key={cue.seq}>{cue.timecode} · {cue.instruction}</Typography.Paragraph>)}
    </Card>
    <Card size="small" title="封面与标题">
      {guide.titles.map((title: string) => <Typography.Paragraph key={title}>{title}</Typography.Paragraph>)}
      <Typography.Paragraph type="secondary">{guide.cover_selection_note}</Typography.Paragraph>
      <Space wrap>{guide.covers.map((cover: any) => <div key={cover.frame} style={{ maxWidth: 250 }}>
        <Image width={160} src={api.fileUrl(directory + cover.frame, run)} />
        <div>{cover.media} #{cover.shot_idx}</div>
        <div style={{ fontSize: 12, color: "#8a93a3" }}>{cover.source_timecode ?? "时间未知"} · {
          cover.motion_source === "frame" ? `帧运动度 ${cover.motion}` : "帧峰值未知"}</div>
        <div style={{ fontSize: 12, color: "#8a93a3" }}>源帧 {cover.source_frame ?? "未知"} · {cover.source_timing_note}</div>
      </div>)}</Space>
    </Card>
    <Card size="small" title="交付与质量门槛">
      <Typography.Paragraph>{guide.delivery.label} · {guide.delivery.width}×{guide.delivery.height} · {guide.delivery.fps}fps · {guide.delivery.bitrate_mbps}Mbps · 目标≤{guide.delivery.target_max_seconds}s</Typography.Paragraph>
      <Typography.Paragraph>{guide.delivery.subtitle_safe_area}</Typography.Paragraph>
      <Typography.Paragraph type="secondary">{guide.delivery.note}</Typography.Paragraph>
      {guide.delivery.checks?.map((check: any, i: number) => <Typography.Paragraph key={i}
        type={check.status === "pass" ? "success" : "warning"}>{check.message}</Typography.Paragraph>)}
      {guide.delivery.upload_limit && <Typography.Paragraph type="secondary">
        发布限制资料核对于 {guide.delivery.upload_limit.references_checked_at}；适用范围如下。
      </Typography.Paragraph>}
      {guide.delivery.upload_limit?.references?.map((ref: any) => <Typography.Paragraph key={ref.url}>
        <a href={ref.url} target="_blank" rel="noreferrer">{ref.title}</a> · {ref.scope}：{ref.note}
      </Typography.Paragraph>)}
      <Typography.Paragraph type="secondary">已核对 {review?.items.filter((item) => item.checked).length ?? 0}/{guide.checklist.length} 项。状态保存在当前任务；单独下载核对记录时，请与粗剪方案和 frames/ 放在同一目录。</Typography.Paragraph>
      {review?.reset_for_new_guide && <Typography.Paragraph type="warning">指导内容已变化，原核对状态未沿用，请重新检查。</Typography.Paragraph>}
      {reviewError && <Space wrap><Typography.Text type="danger">核对记录读取失败：{reviewError}</Typography.Text>
        <Button size="small" onClick={() => api.review(run).then((value) => { setReview(value); setReviewError(null); })
          .catch((error) => setReviewError(String(error)))}>重试</Button></Space>}
      <Space direction="vertical">{guide.checklist.map((item: any, i: number) => <Checkbox key={i}
        checked={review?.items[i]?.checked ?? false}
        disabled={saving || !review || review.items[i]?.text !== item.text}
        onChange={(event) => void updateCheck(i, event.target.checked)}>{item.text}</Checkbox>)}</Space>
    </Card>
  </Space>;
}

export function StagePanels({
  runId,
  stage,
  art,
  mediaDir,
  version = "0",
  arts = {},
  onEditRow,
}: {
  runId: string;
  stage: string;
  art: Record<string, unknown>;
  mediaDir?: string;
  version?: string;
  arts?: Record<string, unknown>;
  onEditRow?: (seq: number) => void;
}) {
  switch (stage) {
    case "scan_media":
      return <ScanPanel art={art} run={runId} mediaDir={mediaDir} />;
    case "plan_segments":
      return <PlanPanel art={art} />;
    case "understand_media":
      return <UnderstandPanel art={art} run={runId} />;
    case "explore_web":
      return <WebPanel art={art} run={runId} onEditRow={onEditRow} />;
    case "pick_music":
      return <MusicPanel art={art} run={runId} />;
    case "write_doc":
      return <DocPanel art={art} run={runId} version={version} />;
    case "finishing_guide":
      return <FinishingPanel art={art} run={runId} version={version} onEditRow={onEditRow} />;
    case "build_timeline":
      return <Space direction="vertical" style={{ width: "100%" }}>
        <Storyboard arts={{ scan_media: arts.scan_media, understand_media: arts.understand_media, build_timeline: art }} runId={runId} />
        <CritiquePanel report={art.critique as Parameters<typeof CritiquePanel>[0]["report"]} />
      </Space>;
    default:
      return <pre>{JSON.stringify(art, null, 2)}</pre>;
  }
}
