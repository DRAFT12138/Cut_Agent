// Cut Agent API 客户端（对应 server.py 路由）

export interface StageMeta {
  name: string;
  status: string; // pending|running|done|degraded|paused|failed
  started_at?: number;
  finished_at?: number;
  elapsed?: number;
  error?: string;
  warnings?: string[];
  progress?: { done: number; total: number; item?: string };
}

export interface RunMeta {
  id: string;
  status: string; // pending|running|paused|done|interrupted|canceled|failed
  created: number;
  updated?: number;
  stages: StageMeta[];
  stages_done: number;
  resumable_from?: string | null;
  error?: string | null;
  has_artifacts?: Record<string, boolean>;
  artifact_versions?: Record<string, string>;
  control_requested?: "pause" | "cancel" | null;
  export_pending?: boolean;
  media_dir?: string;
  copy?: string;
  seed?: number | null;
}

export interface RunListItem {
  seed?: number | null;
  comparison_group?: string;
  id: string;
  status: string;
  created: number;
  stages_done: number;
  stages_total: number;
  copy_head: string;
  resumable_from?: string | null;
}

export interface EventRec {
  seq: number;
  ts: number;
  stage: string;
  type: "status" | "progress" | "log";
  [k: string]: unknown;
}

export interface ReviewState {
  plan_revision: number;
  guide_digest: string;
  items: { text: string; checked: boolean }[];
  updated_at: number | null;
  reset_for_new_guide: boolean;
}

export interface SourceCheck {
  revision: number;
  pictures: {
    seq: number; name: string; source: "local" | "web"; path: string | null;
    size_bytes: number | null; availability: "available" | "missing" | "manual";
    snapshot: "match" | "changed" | "missing" | "unrecorded" | "not_applicable";
    source_url: string | null;
  }[];
  music: {
    role: "preview" | "alternative"; title: string; path: string | null;
    size_bytes: number | null; availability: "available" | "missing";
  }[];
}

async function j<T>(p: string, init?: RequestInit): Promise<T> {
  const r = await fetch(p, init);
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const b = await r.json();
      msg = b.detail || JSON.stringify(b);
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

export const api = {
  stages: () => j<{ stages: string[]; terminal: string[] }>("/api/stages"),
  listRuns: () => j<RunListItem[]>("/api/runs"),
  createRun: (media_dir: string, copy: string, seed?: number, preview = false, platform = "douyin") =>
    j<{ run_id: string; status: string }>("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_dir, copy, seed: seed ?? null, preview, platform }),
    }),
  run: (id: string) => j<RunMeta>(`/api/runs/${encodeURIComponent(id)}`),
  stage: (id: string, name: string) =>
    j<Record<string, unknown>>(
      `/api/runs/${encodeURIComponent(id)}/stages/${name}`
    ),
  events: (id: string, since: number) =>
    j<{ events: EventRec[]; latest: number }>(
      `/api/runs/${encodeURIComponent(id)}/events?since=${since}`
    ),
  review: (id: string) => j<ReviewState>(`/api/runs/${encodeURIComponent(id)}/review`),
  setReview: (id: string, body: { expected_revision: number; index: number; text: string; checked: boolean }) =>
    j<ReviewState>(`/api/runs/${encodeURIComponent(id)}/review`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),
  reviewUrl: (id: string) => `/api/runs/${encodeURIComponent(id)}/review.md`,
  sourceCheck: (id: string) => j<SourceCheck>(`/api/runs/${encodeURIComponent(id)}/sources`),
  sourcesUrl: (id: string) => `/api/runs/${encodeURIComponent(id)}/sources.md`,
  handoffUrl: (id: string) => `/api/runs/${encodeURIComponent(id)}/handoff.zip`,
  pause: (id: string) =>
    j<{ run_id: string; status: string }>(`/api/runs/${id}/pause`, { method: "POST" }),
  cancel: (id: string) =>
    j<{ run_id: string; status: string }>(`/api/runs/${id}/cancel`, { method: "POST" }),
  resume: (id: string) =>
    j<{ run_id: string; status: string }>(`/api/runs/${id}/resume`, { method: "POST" }),
  fileUrl: (path: string, run?: string) =>
    `/api/file?path=${encodeURIComponent(path)}${run ? `&run=${encodeURIComponent(run)}` : ""}`,
};

export const STAGE_LABELS: Record<string, string> = {
  scan_media: "扫描素材",
  plan_segments: "文案分段",
  understand_media: "视觉理解",
  build_timeline: "时间线编排",
  explore_web: "网络素材",
  pick_music: "配乐",
  write_doc: "文档产出",
  finishing_guide: "精剪指导",
};

export const STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  pending: "待启动",
  paused: "已暂停",
  done: "完成",
  recovering: "恢复编辑中",
  interrupted: "中断（可恢复）",
  canceled: "已取消",
  failed: "失败",
};

export function fmtDur(s?: number): string {
  if (s == null || isNaN(s)) return "-";
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s % 60)}s`;
}
