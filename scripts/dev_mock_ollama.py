"""Development stand-in for an Ollama server (UI work without a GPU).

uv run python scripts/dev_mock_ollama.py --port 11500
Point a chat endpoint at it (AGENTOS_CHAT_ENDPOINTS or [chat] in agentos.toml). Pair
scripts/dev_mock_node.py as the generation node to try the studio end to end.
Answers are canned synthetic content; structured requests (cast/storyboard JSON) return valid data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODELS = {
    "demo-moe:30b": {"caps": ["completion", "tools", "thinking"], "family": "moe", "size": "30B",
                     "quant": "Q4_K_M", "bytes": 19_000_000_000, "ctx": 131072},
    "demo-dense:14b": {"caps": ["completion", "tools", "thinking"], "family": "dense", "size": "14B",
                       "quant": "Q4_K_M", "bytes": 9_000_000_000, "ctx": 65536},
    "demo-vision:7b": {"caps": ["completion", "vision"], "family": "vision", "size": "7B", "quant": "Q4_K_M",
                       "bytes": 5_500_000_000, "ctx": 32768},
    "demo-small:4b": {"caps": ["completion", "tools"], "family": "dense", "size": "4B", "quant": "Q4_K_M",
                      "bytes": 2_500_000_000, "ctx": 32768},
    "demo-embed:latest": {"caps": ["embedding"], "family": "bert", "size": "560M", "quant": "F16",
                          "bytes": 1_200_000_000, "ctx": 8192},
}
loaded: set[str] = {"demo-moe:30b"}

SCRIPT = """# 末班车
## 场1 小镇车站 · 雨夜
【画面】站台的灯在雨里发黄，阿岚收起伞，看着空荡荡的铁轨。
阿岚（低落）：末班车……今天也不来了吗？
【画面】老站长提着马灯从值班室走出来，把一杯热茶递给她。
老站长（平静）：会来的。它每晚都会晚十分钟。
## 场2 站台尽头 · 夜
【画面】远处亮起一束车灯，雨丝在光里变成银线，阿岚站了起来。
阿岚（惊讶）：您怎么知道？
老站长（高兴）：因为四十年前，我也在这里等过一个人。
【画面】列车缓缓进站，车门打开，列车员探出身来。
列车员（平静）：最后一班，上车吗？
"""

CAST = {
    "title": "末班车", "logline": "雨夜小站，女孩和老站长一起等一班总是晚点的末班车",
    "characters": [
        {"name": "阿岚", "role": "主角", "description": "二十岁女生，齐肩黑发，米色风衣，背帆布包，安静但固执",
         "visual_prompt": "young woman, shoulder-length black hair, beige trench coat, canvas backpack",
         "voice": "二十岁女声，轻柔，语速偏慢"},
        {"name": "老站长", "role": "配角", "description": "七十岁左右，白发整齐，深蓝制服和旧式帽子，提着马灯",
         "visual_prompt": "elderly station master, neat white hair, navy uniform and cap, holding an oil lantern",
         "voice": "苍老男声，温和，带一点笑意"},
        {"name": "列车员", "role": "群演", "description": "三十多岁，深绿色制服，戴手套",
         "visual_prompt": "train conductor in his thirties, dark green uniform, white gloves", "voice": "中年男声，干脆"},
    ],
    "locations": [
        {"name": "小镇车站", "description": "单站台的小车站，木质站牌，雨夜昏黄灯光",
         "visual_prompt": "small rural train station platform at night in the rain, wooden sign, warm yellow lamps"},
        {"name": "站台尽头", "description": "站台尽头延伸进黑暗的铁轨，远处有车灯",
         "visual_prompt": "end of a wet platform, rails disappearing into darkness, distant train headlight"},
    ],
}


def shots_for(scene: str) -> dict[str, Any]:
    if "场2" in scene or "站台尽头" in scene:
        return {"shots": [
            {"location": "站台尽头", "characters": ["阿岚"], "size": "全景", "camera": "pull_out",
             "action": "远处亮起车灯，雨丝在光里发亮，阿岚站起身", "motion": "i2v", "duration": 3.4,
             "image_prompt": "a distant headlight cuts through the rain, the young woman stands up on the platform",
             "motion_prompt": "rain streaks glow in the light, she rises slowly",
             "lines": [{"speaker": "阿岚", "text": "您怎么知道？", "emotion": "惊讶", "intensity": 0.6}]},
            {"location": "站台尽头", "characters": ["老站长"], "size": "近景", "camera": "push_in",
             "action": "老站长望着车灯，露出笑容", "motion": "still", "duration": 3.6,
             "image_prompt": "the elderly station master smiles at the approaching light, lantern glow on his face",
             "motion_prompt": "", "lines": [{"speaker": "老站长", "text": "因为四十年前，我也在这里等过一个人。",
                                              "emotion": "高兴", "intensity": 0.5}]},
            {"location": "小镇车站", "characters": ["列车员"], "size": "中景", "camera": "static",
             "action": "列车进站，车门打开，列车员探出身", "motion": "still", "duration": 2.8,
             "image_prompt": "a train door opens, the conductor leans out under the platform lamp",
             "motion_prompt": "", "lines": [{"speaker": "列车员", "text": "最后一班，上车吗？", "emotion": "平静"}]},
        ]}
    return {"shots": [
        {"location": "小镇车站", "characters": ["阿岚"], "size": "全景", "camera": "push_in",
         "action": "阿岚收起伞，望着空荡荡的铁轨", "motion": "still", "duration": 3,
         "image_prompt": "the young woman folds her umbrella on an empty rainy platform, looking down the rails",
         "motion_prompt": "rain falls steadily, her coat flutters",
         "lines": [{"speaker": "阿岚", "text": "末班车……今天也不来了吗？", "emotion": "低落", "intensity": 0.6}]},
        {"location": "小镇车站", "characters": ["老站长", "阿岚"], "size": "中景", "camera": "pan_right",
         "action": "老站长提着马灯走来，把热茶递给阿岚", "motion": "i2v", "duration": 3.4,
         "image_prompt": "the station master walks over with a lantern and hands her a steaming cup of tea",
         "motion_prompt": "he walks in and offers the cup, steam rises",
         "lines": [{"speaker": "老站长", "text": "会来的。它每晚都会晚十分钟。", "emotion": "平静"}]},
    ]}


ANSWER = """好的，这里给你一个能直接用的方案：

## 开场 3 秒抓人
1. **反差开头**：空站台和准点显示屏同框，一句「今天也不来了」点出等待。
2. **异常发生**：远处的车灯*只在她抬头时*亮起，镜头从全景急推到特写。
3. **钩子**：老站长那句「我也在这里等过一个人」直接抛出悬念。

| 镜头 | 景别 | 时长 |
| --- | --- | --- |
| 空站台 | 全景 | 3 秒 |
| 递热茶 | 中景 | 3.5 秒 |
| 车灯亮起 | 全景 | 3.5 秒 |

时长估算可以用 $t = \\frac{n}{4.5} + 0.8$（$n$ 是台词字数）。

```python
def shot_seconds(text: str) -> float:
    return len(text) / 4.5 + 0.8
```
需要的话我可以继续把整场戏拆成分镜表。"""

THINKING = "用户想要一个短剧开场。先确定冲突：等不到的末班车和知道答案的老站长。三秒内要有反差和悬念，所以先给结论再列镜头，最后补一个时长估算公式。"

app = FastAPI()


@app.get("/api/version")
async def version() -> dict[str, str]:
    return {"version": "0.0.0-mock"}


@app.get("/api/tags")
async def tags() -> dict[str, Any]:
    return {"models": [{"name": name, "model": name, "size": m["bytes"],
                        "details": {"family": m["family"], "parameter_size": m["size"], "quantization_level": m["quant"]}}
                       for name, m in MODELS.items()]}


@app.get("/api/ps")
async def ps() -> dict[str, Any]:
    return {"models": [{"name": n, "model": n, "size_vram": int(MODELS[n]["bytes"] * 0.42)} for n in sorted(loaded)]}


@app.post("/api/show")
async def show(request: Request) -> Any:
    body = await request.json()
    meta = MODELS.get(body.get("model", ""))
    if meta is None:
        return JSONResponse({"error": "model not found"}, status_code=404)
    return {"capabilities": meta["caps"], "model_info": {f"{meta['family']}.context_length": meta["ctx"]}}


@app.post("/api/generate")
async def generate(request: Request) -> dict[str, Any]:
    body = await request.json()
    if body.get("keep_alive") == 0:
        loaded.discard(body.get("model", ""))
    return {"done": True}


async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
    model = body["model"]
    first_load = model not in loaded
    if first_load:
        await asyncio.sleep(1.2)
    loaded.add(model)
    messages = body.get("messages") or []
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    last = messages[-1]["content"] if messages else ""
    started = time.monotonic()
    if body.get("format"):
        schema = json.dumps(body["format"])
        if "美术设定" in system:
            text = json.dumps(CAST, ensure_ascii=False)
        elif "分镜师" in system and "image_prompt" in schema and "shots" in schema:
            text = json.dumps(shots_for(last), ensure_ascii=False)
        else:
            text = json.dumps({"image_prompt": "refined cinematic prompt: " + re.sub(r"\s+", " ", last)[:120],
                               "motion_prompt": "subtle natural motion, hair and cloth sway"}, ensure_ascii=False)
        pieces = [text[i:i + 40] for i in range(0, len(text), 40)]
        thinking: list[str] = []
    elif "编剧" in system:
        pieces = [SCRIPT[i:i + 6] for i in range(0, len(SCRIPT), 6)]
        thinking = []
    else:
        pieces = re.findall(r".{1,3}", ANSWER, flags=re.S)
        thinking = re.findall(r".{1,4}", THINKING, flags=re.S) if body.get("think") else []
    for chunk in thinking:
        yield (json.dumps({"model": model, "message": {"role": "assistant", "content": "", "thinking": chunk},
                           "done": False}, ensure_ascii=False) + "\n").encode()
        await asyncio.sleep(0.025)
    for chunk in pieces:
        yield (json.dumps({"model": model, "message": {"role": "assistant", "content": chunk}, "done": False},
                          ensure_ascii=False) + "\n").encode()
        await asyncio.sleep(0.02 if not body.get("format") else 0.004)
    elapsed = time.monotonic() - started
    count = len(pieces) * 2
    yield (json.dumps({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "stop", "eval_count": count, "eval_duration": int(elapsed * 1e9),
                       "prompt_eval_count": 180, "load_duration": int((1.2 if first_load else 0.05) * 1e9)},
                      ensure_ascii=False) + "\n").encode()


@app.post("/api/chat")
async def chat(request: Request) -> Any:
    body = await request.json()
    if body.get("model") not in MODELS:
        return JSONResponse({"error": f"model '{body.get('model')}' not found"}, status_code=404)
    if body.get("think") and "thinking" not in MODELS[body["model"]]["caps"]:
        return JSONResponse({"error": f"\"{body['model']}\" does not support thinking"}, status_code=400)
    return StreamingResponse(stream(body), media_type="application/x-ndjson")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11500)
    uvicorn.run(app, host="127.0.0.1", port=parser.parse_args().port, log_level="warning")
