from pathlib import Path

from cut_agent import vision
from cut_agent.visual_index import build, search


def test_structured_labels_keep_only_supported_tags_and_grounded_evidence(tmp_path, monkeypatch):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"image")
    shot = vision.Shot(1, 0, 2, 0, 1, frames=[frame], frame_times=[.5])
    vv = vision.VideoVision("clip.mp4", Path("clip.mp4"), 2, shots=[shot], frames=[frame])
    monkeypatch.setattr(vision, "chat_vision", lambda *args, **kwargs: '''{
      "shots":[{"shot_idx":1,"description":"人物向左奔跑","roles":["高潮"],
      "tags":{"subjects":["人物"],"actions":["奔跑"],"shot_sizes":["全景"],"invented":["x"]},
      "evidence":[{"tag":"奔跑","frame_time":0.5,"confidence":0.8},
                  {"tag":"猜测","frame_time":1.5,"confidence":0.2},
                  {"tag":"人物","frame_time":0.5,"confidence":2}]}]}''')

    vision.describe_shots(vv)

    assert shot.tags == {"subjects": ["人物"], "actions": ["奔跑"], "shot_sizes": ["全景"]}
    assert shot.tag_evidence == [
        {"tag": "奔跑", "frame_time": .5, "confidence": .8, "source": "vision_model"}]


def test_compound_search_is_stable_and_supports_semantics_entities_actions_and_composition():
    media = [{"name": "a.mp4", "shots": [
        {"idx": 1, "start": 0, "end": 2, "description": "城市中的人物奔跑", "roles": ["高潮"],
         "tags": {"subjects": ["人物"], "actions": ["奔跑"], "scenes": ["城市"],
                  "shot_sizes": ["全景"], "camera_angles": [], "text_regions": []},
         "tag_evidence": [{"tag": "奔跑", "frame_time": .5, "confidence": .9}]},
        {"idx": 2, "start": 2, "end": 4, "description": "人物休息", "roles": ["收尾"],
         "tags": {"subjects": ["人物"], "actions": ["休息"], "shot_sizes": ["特写"]}},
    ]}]

    index = build(media)
    results = search(index, "城市 高潮", entities=["人物"], actions=["奔跑"], composition=["全景"])

    assert [row["id"] for row in results] == ["a.mp4#1"]
    assert build(media) == index
