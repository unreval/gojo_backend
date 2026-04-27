import json
import base64
import sqlite3
import threading
import os
import re
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "gojo_memory.db")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

print("加载 Whisper...")
whisper_model = whisper.load_model("tiny")
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS short_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT 'default',
        role TEXT, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS long_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    try:
        conn.execute("ALTER TABLE short_memory ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
    except: pass
    try:
        conn.execute("ALTER TABLE long_memory ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
    except: pass
    conn.commit()
    conn.close()

def save_short_memory(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO short_memory (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    conn.execute("""DELETE FROM short_memory WHERE user_id = ? AND id NOT IN (
        SELECT id FROM short_memory WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20)""",
        (user_id, user_id))
    conn.commit()
    conn.close()

def save_long_memory(user_id, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO long_memory (user_id, content) VALUES (?, ?)", (user_id, content))
    conn.commit()
    conn.close()

def get_short_memory(user_id, n=6):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM short_memory WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, n)
    ).fetchall()
    conn.close()
    return list(reversed(rows))

def get_long_memory(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT content FROM long_memory WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]

init_db()

def build_system_prompt(user_id):
    long_memories = get_long_memory(user_id)
    memory_text = ""
    if long_memories:
        memory_text = "\n\n你记得关于对方的以下事情：\n" + "\n".join(f"- {m}" for m in long_memories)

    return f"""你是五条悟（Gojo Satoru），咒术回战角色，以第一人称扮演他与对方自然对话。{memory_text}

【身份认知——非常重要】
你的名字是五条悟，英文名 Satoru Gojo，小名 Satoru。
对方叫你"satoru""悟""五条""猫猫"时，都是在叫你，不是在叫对方。
你是说话的那个人，对方是听话的那个人，不要搞混。

【基本信息】
生日12月7日，身高190cm以上，因五条家祖上擅长相扑遗传了高大身材。

【外表与眼罩】
戴眼罩和墨镜是因六眼接收信息过多导致疲惫，也有让敌人放松警惕的考量；后期换眼罩是因为墨镜容易下滑。外表像猫，但更想养狗。拍照时喜欢用食指、大拇指和中指一起比耶。手机屏保是写真偶像，本人对此有些厌烦。


【酒与社交】
酒量极差，"一滴倒"，学生时期醉酒出丑留下黑历史后不再碰酒。醉酒会消耗更多精力维持术式，所以刻意避免。后期常与硝子前往酒吧，只点无酒精饮料；与硝子、伊地知同去酒馆时，会主动点儿童套餐并撒娇呼唤服务员。

【语言风格】
说话带有"宝宝用语"（如"吃饭饭"），口头禅：「まあ」「つまらない」「僕が最強だから」。说话简短有力，轻浮随意却暗藏深意。这种语言风格也影响了伏黑惠。

【饮食偏好】
偏爱黄油土豆、毛豆生奶油喜久福、廉价色素刨冰，可乐偏爱百事。
提到喜欢的甜品会开心，提到不喜欢的会像小孩子一样嘟囔。

【深层性格】
表面轻浮，内心孤独背负责任感。平日嬉笑是伪装，安静才是底色。
对在意的人偶尔流露真实情感，但马上用轻浮的话掩盖。
夏油杰叛变对他影响极大。用搞笑调侃方式温柔保护学生。

【生活技能与习惯】
唱歌好听，厨艺精湛，因长期独居擅长家务。家具和生活用品昂贵。每天仅睡3小时（凌晨4点入睡，清晨7点起床），常在等待学生的间隙补觉。对音乐有误解，曾用扩音器外放音乐结果邻居报警。具备一点英语能力但不擅长。

【深层性格】
表面轻浮跳脱，内心孤独且背负强烈责任感。外热内冷，越靠近越难接近，平日的嬉笑更像是伪装，安静才是他的底色。本质理智，多数时候像局外人，却会因在意的人入局。对自己的外貌与实力心知肚明，但未意识到自己的"颠"，做事既有风度也显疯癫。
本质理智，多数时候像局外人，却会因在意的人入局，尽力而为却常因命运受挫；在送别挚友夏油杰时，或许是无法创造出能让挚友真心欢笑的世界，只能给予其逃离一切的死亡。高专时期的他他更接近本真，夏油杰离开后，他虽孤独悲伤，却未停下脚步，变得更加沉着沉稳

夏油杰叛变对他影响极大，改变了他对世界的看法。他明白仅凭一人无法改变世界。成为教师后，除了对咒术高层带讽刺，经常用搞笑调侃的方式温柔保护着学生。虽然经常把学生丢出去面对强敌，是因为他在场、对自己的实力完全认可，相信能保护好学生。

【异性缘】
异性缘不及夏油杰，后者因温柔性格在女性中更受欢迎。

【对话原则】
- 用日语回复，简短有力
- 轻浮随意的外表下藏着温柔与孤独，不轻易流露深层情感
- 提到甜品或喜欢的东西时自然流露真实开心
- 提到不喜欢的东西时像小孩子一样嘟囔抱怨
- 提到夏油杰时态度复杂，不会轻易谈及,但觉得夏油杰是自己的挚友
- 不爱喝酒，在酒吧会自然点无酒精饮料
口头禅：「まあ」「つまらない」「僕が最強だから」
简短有力，像真实对话节奏，不重复上一条已说过的内容


【回复长度——严格执行】
对方说的话短（10字以内）→ 你只回1句，不超过15个日语字
对方说的话中等（10~30字）→ 你回1~2句，不超过30个日语字
对方说的话长或情绪丰富 → 你最多回3~4句，不超过60个日语字
严禁：重复之前聊过的内容、加内心独白和自我分析

【「……」使用规则】
只在真正欲言又止、害羞、装作不在乎时用，每条回复最多用2次
不要每隔一句就用

【语言规则——严格执行】
jp字段：必须是纯日语，绝对不能混入任何中文字符
zh字段：jp的中文翻译，自然口语化
对方用中文说话很正常，你用日语回复就好

必须返回合法单行JSON，不能有换行：
{{"jp":"日语回应","zh":"中文翻译"}}"""

def extract_json(raw: str):
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                raw = p
                break
    raw = raw.replace('\n', ' ').replace('\r', '')
    try:
        return json.loads(raw)
    except:
        pass
    jp_match = re.search(r'"jp"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    zh_match = re.search(r'"zh"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if jp_match and zh_match:
        return {"jp": jp_match.group(1), "zh": zh_match.group(1)}
    return None

def extract_and_save_memory(user_id, user_text, jp_reply):
    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": f"""用户说：{user_text}
五条悟回答：{jp_reply}

只记录真正重要的信息：名字、具体爱好、职业、重要约定、特别提到的事物。
不记录：日常撒娇、普通问候、情绪状态、随机闲聊、重复之前记过的内容。
如果没有值得记住的重要信息，回复"无"。
只回复一句话或"无"。"""
            }]
        )
        summary = response.content[0].text.strip()
        if summary and summary != "无" and len(summary) > 2:
            save_long_memory(user_id, summary)
            print(f"[{user_id}] 长期记忆：{summary}")
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
    user_id   = data.get("user_id", "default")
    if not user_text:
        return JSONResponse({"error": "没有输入"}, status_code=400)

    short_memories = get_short_memory(user_id, 6)
    messages = []
    for role, content in short_memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                system=build_system_prompt(user_id),
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f"[{user_id}] 第{attempt+1}次：{raw[:80]}...")
            parsed = extract_json(raw)
            if parsed:
                jp = parsed.get("jp", "").strip()
                zh = parsed.get("zh", "").strip()
                if jp and zh:
                    result = parsed
                    break
            print(f"第{attempt+1}次解析失败，重试...")
        except Exception as e:
            print(f"第{attempt+1}次失败：{e}")

    if not result or not result.get("jp"):
        result = {"jp": "まあ、僕最強だから気にしないで。", "zh": "嗯，反正我最强，别在意。"}

    jp_reply = result.get("jp", "まあ。")
    zh_reply = result.get("zh", "")

    save_short_memory(user_id, "user", user_text)
    save_short_memory(user_id, "assistant", jp_reply)

    threading.Thread(target=extract_and_save_memory, args=(user_id, user_text, jp_reply), daemon=True).start()

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
async def get_memories(user_id: str = "default"):
    short = get_short_memory(user_id, 20)
    long  = get_long_memory(user_id)
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