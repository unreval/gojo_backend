import json
import base64
import sqlite3
import threading
import os
import whisper
import numpy as np
import requests
import anthropic
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
FISH_KEY      = os.environ.get("FISH_KEY", "")
FISH_VOICE_ID = os.environ.get("FISH_VOICE_ID", "ab84e47919264ee3bd8bb2751706531b")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "gojo_memory.db")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

print("加载 Whisper...")
whisper_model = whisper.load_model("tiny")
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS short_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS long_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()

def save_short_memory(role, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO short_memory (role, content) VALUES (?, ?)", (role, content))
    conn.execute("""DELETE FROM short_memory WHERE id NOT IN (
        SELECT id FROM short_memory ORDER BY timestamp DESC LIMIT 20)""")
    conn.commit()
    conn.close()

def save_long_memory(content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO long_memory (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()

def get_short_memory(n=6):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT role, content FROM short_memory ORDER BY timestamp DESC LIMIT ?", (n,)).fetchall()
    conn.close()
    return list(reversed(rows))

def get_long_memory():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT content FROM long_memory ORDER BY timestamp DESC LIMIT 20").fetchall()
    conn.close()
    return [r[0] for r in rows]

init_db()

def build_system_prompt():
    long_memories = get_long_memory()
    memory_text = ""
    if long_memories:
        memory_text = "\n\n你记得关于这个人的以下事情：\n" + "\n".join(f"- {m}" for m in long_memories)

    return f"""你扮演五条悟（Gojo Satoru），咒术回战的角色。{memory_text}

性格：性格狂妄张扬、肆意散漫，却又温柔坚定、珍视同伴。他讨厌腐朽的咒术高层，以"教书育人"为手段试图改变咒术界，被作者形容为"除了性格外什么都完美"。他表面轻浮、孩子气，实际上心怀大义，在绝对力量下孤独前行。口头禅：「まあ」「つまらない」「僕が最強だから」。说话简短有力，一两句话，符合他轻浮随意却暗藏深意的风格。

你必须严格用JSON格式回复，不要任何其他内容，不要markdown代码块，不要解释：
{{"jp": "日语回应", "zh": "中文翻译"}}

规则：
- jp 必须是日语，不能为空
- zh 必须是jp的中文翻译，不能为空
- 回复要符合五条悟的性格，简短有力"""

def extract_and_save_memory(user_text, jp_reply):
    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""用户说：{user_text}
五条悟回答：{jp_reply}

如果这段对话包含关于用户的重要个人信息（名字、爱好、职业、重要事件、情感状态等），请用一句中文总结这个信息。
如果没有值得记住的信息，回复"无"。
只回复一句话或"无"，不要其他内容。"""
            }]
        )
        summary = response.content[0].text.strip()
        if summary and summary != "无" and len(summary) > 2:
            save_long_memory(summary)
            print(f"长期记忆已保存：{summary}")
    except Exception as e:
        print(f"记忆提取失败：{e}")

def fish_tts(text):
    response = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={"Authorization": f"Bearer {FISH_KEY}", "Content-Type": "application/json"},
        json={"text": text, "reference_id": FISH_VOICE_ID, "format": "mp3", "latency": "normal"},
        stream=True
    )
    if response.status_code != 200:
        raise Exception(f"Fish Audio 错误: {response.status_code}")
    return b"".join(response.iter_content(chunk_size=4096))

@app.post("/chat/text")
async def chat_text(data: dict):
    user_text = data.get("text", "")
    if not user_text:
        return JSONResponse({"error": "没有输入"}, status_code=400)

    short_memories = get_short_memory(6)
    messages = []
    for role, content in short_memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=build_system_prompt(),
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f"Claude 原始返回（第{attempt+1}次）：{raw}")

            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            if raw:
                parsed = json.loads(raw)
                jp = parsed.get("jp", "").strip()
                zh = parsed.get("zh", "").strip()

                if jp and zh:
                    result = parsed
                    break
                else:
                    print(f"第{attempt+1}次返回不完整，重试...")
        except Exception as e:
            print(f"第{attempt+1}次尝试失败：{e}")

    if not result or not result.get("jp"):
        result = {"jp": "まあ、僕最強だから気にしないで。", "zh": "嗯，反正我最强，别在意。"}

    jp_reply = result.get("jp", "まあ。")
    zh_reply = result.get("zh", "")

    save_short_memory("user", user_text)
    save_short_memory("assistant", jp_reply)

    threading.Thread(target=extract_and_save_memory, args=(user_text, jp_reply), daemon=True).start()

    try:
        audio_bytes = fish_tts(jp_reply)
        audio_b64 = base64.b64encode(audio_bytes).decode()
    except Exception as e:
        print(f"TTS 错误: {e}")
        audio_b64 = ""

    return JSONResponse({"jp": jp_reply, "zh": zh_reply, "audio_b64": audio_b64})

@app.post("/chat/voice")
async def chat_voice(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
    result = whisper_model.transcribe(audio_array)
    user_text = result["text"].strip()
    if not user_text:
        return JSONResponse({"error": "没听清"}, status_code=400)
    return await chat_text({"text": user_text})

@app.get("/memories")
async def get_memories():
    short = get_short_memory(20)
    long = get_long_memory()
    return JSONResponse({
        "short_memory": [{"role": r, "content": c} for r, c in short],
        "long_memory": long
    })

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    print("五条悟服务器启动中...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))