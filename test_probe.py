"""自检：媒体探测 + LLM 连通性。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from cut_agent.llm import chat_json, LLMError  # noqa: E402
from cut_agent.media import probe_media  # noqa: E402


def main() -> None:
    root = Path(__file__).parent
    items = probe_media(root / "sample_media")
    print(f"扫描到 {len(items)} 个素材：")
    for it in items:
        cuts = [round(c, 1) for c in it.scene_cuts]
        print(f"  {it.name} [{it.kind}] {it.duration:.1f}s {it.width}x{it.height} cuts={cuts}")

    print("--- LLM 连通性 ---")
    try:
        r = chat_json("只输出合法 JSON", '输出 {"ok": true, "msg": "hi"}', max_tokens=200)
        print("LLM OK:", r)
    except LLMError as e:
        print("LLM FAIL:", e)


if __name__ == "__main__":
    main()
