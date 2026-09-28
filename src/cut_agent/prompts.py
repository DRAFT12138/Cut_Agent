"""LangGraph 状态与提示词。"""
from __future__ import annotations

from typing import Any, TypedDict


class CutState(TypedDict, total=False):
    media_folder: str
    copy: str                       # 用户文案
    media: list[dict]               # 本地扫描结果: {name,kind,duration,width,height,
                                    #  scene_cuts, shots:[{idx,start,end,cut_score,motion,
                                    #  frame_times}], description(视频镜头描述)}
    segments: list[dict]            # 文案切段: {text, mood, duration, keywords}
    timeline: list[dict]            # 粗剪时间线: {seq, media, kind, source, use_duration,
                                    #  start_offset, segment_text, note}
    critique: dict
    web_assets: list[dict]          # 网络素材: {name, url, query, for_segment}
    music: dict                     # {primary, alternatives, mood, downloads}
    doc_path: str
    line_doc_path: str
    log: list[str]


# ---------- 提示词 ----------

PLAN_SYSTEM = """你是资深短视频剪辑师兼分镜师。你理解粗剪的本质：粗剪是第一遍结构搭建——
按叙事逻辑把内容排成"开头-发展-高潮-结尾"，目标是"没有音乐和字幕也能看懂讲了什么"，
不追求画面好看（那是精剪的事）。
根据用户文案，把它切分成适合粗剪的片段（3~10 段），每段配一句旁白、一个画面情绪、建议时长、
以及用于找画面的关键词（中文+英文各给，方便网络搜索补素材）。
要求：
- 首段负责"开头"：用最强的信息点或画面钩子抓住观众，时长可略短；
- 中段按叙事推进，情绪有变化（不要连续多段同一种情绪）；
- 末段负责"结尾"：收束或留钩子；
- 每段时长 = 该段旁白朗读时长（中文约每秒 4.5 字），允许 ±15% 浮动。
只输出 JSON，结构：
{"segments":[{"text":"旁白原文","mood":"情绪/节奏","role":"开头|发展|高潮|收尾","intensity":1到5数字,"duration":建议秒数(数字),"kw_cn":[".."],"kw_en":[".."]}]}
时长合计尽量等于 文案朗读时长 的 0.8~1.2 倍（中文约每秒 4~5 字）。"""

MATCH_SYSTEM = """你是粗剪时间线编排师。粗剪的目标是搭好叙事结构：按"开头-发展-高潮-结尾"
把素材排进时间线，保证故事完整、信息连续，而不是画面精致（精剪阶段才处理转场/调色/节奏微调）。
给你：本地素材清单（视频含"镜头描述"——按画面变化抽帧后的内容理解，以及镜头区间
start-end 秒；图片含文件名与元数据）、以及文案分段。为每一段挑选一个本地素材
（优先视频，画面感强的图片次之）：
- 选素材先看"镜头描述"是否与该段旁白内容/情绪相关，相关才用；不相关就转网络素材；
- 有镜头清单的视频：只指定 media（原文件名）与 shot_idx（清单编号），不要计算 offset。
  默认使用一个完整镜头。若旁白较长，可显式 span=true，最多连用三个相邻镜头；
  use_duration 表示期望覆盖时长，程序会按镜头边界确定实际时长。
- 无镜头标注的视频才使用 start_offset 和 use_duration，不能越过素材结尾；
- 图片/网络占位的 use_duration 取旁白建议时长；
- 相邻两行尽量用不同素材/不同角度，避免同画面重复堆叠；
- 若本地素材明显不足或与该段内容不相关，把该段标记 "needs_web": true 并给出 web_query
  （英文关键词优先，描述具体画面，例如 "aerial city timelapse"）；
- 不要重复使用同一段视频的不同片段（除非素材很少）。
只输出 JSON，结构：
{"timeline":[{"seq":1,"media":"文件名","kind":"video|image","source":"local",
 "shot_idx":镜头编号或null,"span":false,"use_duration":数字,"start_offset":数字,"segment_text":"旁白","needs_web":false,"web_query":"关键词"}]}
全部段落都要有，顺序即时间线顺序。"""

MUSIC_SYSTEM = """你是配乐指导。根据视频整体情绪（由文案得出）推荐一首主 BGM 和 2 首备选，
只输出 JSON：{"mood":"..","primary":{"title":"..","artist":"..","reason":".."},
"alternatives":[{"title":"..","artist":"..","reason":".."}]}
风格描述要具体（如：轻快 ukulele、史诗管弦、lo-fi 电子），便于到免版权音乐站搜索。"""

DESCRIBE_MEDIA_SYSTEM = """你是素材标注员。给你视频/图片文件名、时长、分辨率、场景切点，
为每个素材写一句 12 字以内的画面内容推测（根据文件名与元数据合理猜测，不确定就写"见素材"）。
只输出 JSON：{"notes":{"文件名":"一句话"}}"""
