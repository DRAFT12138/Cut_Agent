"""Offline project delivery checks; scoped references are not account upload limits."""
from copy import deepcopy
import math

from .shots import number

PLATFORMS = {
    "douyin": {"label": "抖音", "width": 1080, "height": 1920, "fps": 25, "bitrate_mbps": 12, "target_max_seconds": 60},
    "xiaohongshu": {"label": "小红书", "width": 1080, "height": 1440, "fps": 25, "bitrate_mbps": 12, "target_max_seconds": 90},
    "bilibili": {"label": "B站", "width": 1920, "height": 1080, "fps": 25, "bitrate_mbps": 16, "target_max_seconds": 180},
}
REFERENCES = {
    "douyin": [{"title": "抖音开放平台：上传视频", "url": "https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/upload/",
                "scope": "开放平台上传接口", "max_seconds": 900,
                "note": "接口文档列出 15 分钟限制，不作为当前账号手工发布上限"}],
    "xiaohongshu": [{"title": "小红书分享开放平台：iOS 接入", "url": "https://agora.xiaohongshu.com/doc/ios",
                     "scope": "分享 SDK", "note": "有视频时长限制错误类型，未获得可用于当前账号手工发布的统一数值"}],
    "bilibili": [{"title": "哔哩哔哩开放平台文档", "url": "https://open.bilibili.com/doc",
                  "scope": "开放平台文档入口", "note": "公开入口未提供可确认当前账号手工发布时长上限的证据"}],
}


def build_delivery(state: dict, platform: str) -> dict:
    if platform not in PLATFORMS:
        raise ValueError(f"不支持的平台预设：{platform}")
    result = dict(PLATFORMS[platform])
    width, height = result["width"], result["height"]
    board = state.get("storyboard") or {}
    cards = board.get("cards") or []
    timeline_fps = max(1, number(board.get("timeline_fps"), result["fps"]))
    duration = (number(cards[-1].get("timeline_out_frame")) / timeline_fps if cards else
                sum(max(0, number(row.get("use_duration"))) for row in state.get("timeline", [])))
    over = max(0, duration - result["target_max_seconds"])
    checks = [{"code": "target_duration", "status": "review" if over > .000001 else "pass",
               "actual_seconds": round(duration, 4), "limit_seconds": result["target_max_seconds"],
               "over_seconds": round(over, 4), "message":
               f"成片时间轴 {duration:.2f}s，项目目标≤{result['target_max_seconds']}s；" +
               (f"超出 {over:.2f}s，请缩短或拆分方案" if over > .000001 else "在项目目标内")}]
    if timeline_fps != result["fps"]:
        checks.append({"code": "timeline_fps", "status": "review", "message":
                       f"当前执行卡按 {timeline_fps:g}fps，交付建议为 {result['fps']}fps；正式导出前统一并重核帧锚点"})
    media = {m["name"]: m for m in state.get("media", [])}
    assets = {a.get("local"): a for a in state.get("web_assets", []) if a.get("local")}
    for seq, row in enumerate(state.get("timeline", []), 1):
        source = (assets.get(row.get("local_path") or row.get("ref"), {}) if row.get("source") == "web"
                  else media.get(row.get("media"), {}))
        if source.get("kind", row.get("kind")) in ("video", "image") and not source.get("display_geometry"):
            checks.append({"code": "source_geometry", "seq": seq, "status": "review", "message":
                           f"第 {seq} 行缺少显示方向/像素比例记录，画幅按旧尺寸估算；重新扫描或替换素材后复核"})
        sw, sh = number(source.get("width")), number(source.get("height"))
        if sw <= 0 or sh <= 0:
            checks.append({"code": "source_dimensions", "seq": seq, "status": "review",
                           "message": f"第 {seq} 行缺少有效画幅尺寸，替换/核对素材后检查构图"})
            continue
        if abs((sw / sh) / (width / height) - 1) > .01:
            checks.append({"code": "aspect_ratio", "seq": seq, "status": "review", "message":
                           f"第 {seq} 行 {sw:g}×{sh:g} 与目标 {width}×{height} 比例不同，裁切重构或加底；复核主体和字幕"})
        scale = max(width / sw, height / sh)
        if scale > 1.01:
            checks.append({"code": "upscale", "seq": seq, "status": "review", "message":
                           f"第 {seq} 行铺满目标画幅需放大约 {scale:.2f} 倍，检查清晰度或换高分辨率素材"})
    checks.append({"code": "upload_limit", "status": "unverified", "message":
                   "当前账号手工发布的时长/文件大小上限待确认；请在目标发布入口核对，项目目标通过不代表上传合规"})
    safe = {"left": .08, "right": .08, "top": .10, "bottom": .20}
    rect = [math.ceil(width * safe["left"]), math.ceil(height * safe["top"]),
            math.floor(width * (1 - safe["right"])), math.floor(height * (1 - safe["bottom"]))]
    result.update(platform=platform, preset_source="project", duration_seconds=round(duration, 4), checks=checks,
                  subtitle_safe_margins=safe, subtitle_safe_rect_px=rect,
                  subtitle_safe_area=f"项目建议字幕区（左/上/右/下边界，px）：{rect}；左右留 8%、上 10%、下 20%，发布预览中复核遮挡",
                  upload_limit={"scope": "当前账号手工发布", "status": "unverified", "max_seconds": None,
                                "references_checked_at": "2026-09-13", "references": deepcopy(REFERENCES[platform])},
                  note="分辨率/fps/码率/安全区与目标时长为项目建议；预览只用于粗剪核对，正式导出按本预设处理。官方上传上限另行确认")
    return result
