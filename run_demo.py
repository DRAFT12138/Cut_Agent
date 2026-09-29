"""端到端跑粗剪流水线（示例素材 + 示例文案）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from cut_agent.graph import run  # noqa: E402


def main() -> None:
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    state = run(str(ROOT / "sample_media"), copy)
    print("\n=== 运行日志 ===")
    for l in state.get("log", []):
        print(" -", l)
    print("\n=== 输出 ===")
    print("doc:", state.get("doc_path"))
    print("lines:", state.get("line_doc_path"))
    print("配乐主 BGM:", state.get("music", {}).get("primary", {}).get("title"))
    print("配乐下载:", state.get("music", {}).get("downloads"))
    print("网络素材数:", len(state.get("web_assets", [])))


if __name__ == "__main__":
    main()
