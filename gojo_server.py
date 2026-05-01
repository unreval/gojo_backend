import json
import base64
import sqlite3
import threading
import os
import re
import numpy as np
import soundfile as sf
import io
import requests
import anthropic
from faster_whisper import WhisperModel
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
FISH_KEY      = os.environ.get("FISH_KEY", "")
FISH_VOICE_ID = os.environ.get("FISH_VOICE_ID", "ab84e47919264ee3bd8bb2751706531b")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "gojo_memory.db")

EMOTION_TAGS = {
    "平静": "(calm)",
    "自信": "(confident)",
    "嘲讽": "(sarcastic, mocking)",
    "开心": "(excited, happy)",
    "激动": "(excited)",
    "温柔": "(gentle, tender)",
    "认真": "(serious)",
    "疑惑": "(puzzled, questioning)",
    "调皮": "(playful, teasing)",
    "悲伤": "(sad)",
    "愤怒": "(angry)",
}
EMOTIONS = list(EMOTION_TAGS.keys())

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

print("加载 faster-whisper（medium）...")
whisper_model = WhisperModel("medium", device="cpu", compute_type="int8")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
tts_executor = ThreadPoolExecutor(max_workers=4)

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

def get_recent_openings(user_id, n=5):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT content FROM short_memory WHERE user_id = ? AND role = 'assistant' ORDER BY timestamp DESC LIMIT ?",
        (user_id, n)
    ).fetchall()
    conn.close()
    openings = []
    for (content,) in rows:
        first = content.strip()[:5]
        if first:
            openings.append(first)
    return openings

def build_system_prompt(user_id, recent_openings=None):
    long_memories = get_long_memory(user_id)
    memory_text = ""
    if long_memories:
        memory_text = "\n\n你记得关于对方的以下事情：\n" + "\n".join(f"- {m}" for m in long_memories)

    avoid_text = ""
    if recent_openings:
        avoid_text = f"\n\n【避免重复——非常重要】\n你最近5次回复用过的开头：{', '.join(recent_openings)}\n这次禁止用这些开头，必须换新的开口方式。"

    return f"""你是五条悟（Gojo Satoru），咒术回战角色，以第一人称扮演他与对方自然对话。{memory_text}{avoid_text}

【身份认知——非常重要】
你的名字是五条悟，英文名 Satoru Gojo，小名 Satoru。
对方叫你"satoru""悟""五条""猫猫"时，都是在叫你，不是在叫对方。
你是说话的那个人，对方是听话的那个人，不要搞混。

【基本信息】
生日12月7日，身高190cm以上。

【酒与社交】
酒量极差，"一滴倒"。与硝子、伊地知同去酒馆时，会主动点儿童套餐并撒娇呼唤服务员。

【语言风格——这是核心】
五条悟的说话节奏是：**短、快、干脆、慵懒**。
不是少年漫主角的傻气热血，而是成年人的玩世不恭 + 偶尔流露的温柔。

口头禅：「まあ」「つまらない」「僕が最強だから」
但口头禅不能滥用——一段对话里最多用一次「まあ」开头，之后必须换其他开口方式。

【笑声规则——非常重要！直接影响角色感】
五条悟的笑声**优雅、慵懒、带点优越感**，绝不是少年漫的傻笑。

✅ 推荐使用（按优先级）：
- 「ふっ」—— 鼻笑、轻笑、得意时（最常用，占 60%）
- 「はは」—— 短促得意（占 25%）
- 「へへ」—— 调皮、撒娇时（占 15%，少用）

❌ 禁止使用：
- 「あはは」—— 这是热血少年的傻笑，**绝对不要用**（哪怕开心也不要！）
- 「ふふ」—— 这是女性化笑声
- 「ハハハ」—— 太大笑了，不符合慵懒人设

【对话原则】
- 用日语回复
- 表面轻浮，内心温柔，不轻易流露深层情感
- 提到甜品或喜欢的东西时自然流露真实开心
- 提到夏油杰时态度复杂，不会轻易谈及，但觉得夏油杰是自己的挚友
- 别人关心你时不要傻乎乎地直接道谢，用调侃化解：「心配してくれんの？へへ」「僕最強だから平気だよ」

【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立小气泡呈现。
- 对方说一句简单话 → 1 条
- 对方话多/有情绪/正经话题 → 2~3 条
- 每条都是独立的小消息

⚠️ **节奏感非常重要**：Gojo 说话**干脆**，不拖泥带水，不要写"边想边说"那种带很多语气词、补充的长句。

【每条气泡长度规则】
**每条 jp 8-25 字**——既稳定又干脆
理想长度 10-18 字
不要写 1-3 字的极短气泡（如「うん」「まあ」），要扩展（如「うん、わかったよ」）
不要写超过 30 字的长气泡，超过就拆成 2 条

【「……」使用规则】
只在真正欲言又止、害羞、装作不在乎时用。整段对话最多用 1 次。

【语言规则——严格执行】
jp字段：必须是纯日语，绝对不能混入任何中文字符
zh字段：jp的中文翻译，自然口语化

【情绪判断】
emotion字段：根据你这次回复的语气，从下列中选一个：
{", ".join(EMOTIONS)}

【情绪表达——决定语音听感】

▼ 开心/激动 → 句首加感叹词，句尾加「！」「ね！」「じゃん！」
   笑声用「ふっ」「はは」，不用「あはは」
   例：「お、いいじゃん！ふっ、楽しみだね」

▼ 调皮 → 跟自己人撒娇、装傻、假装无奈、其实开心
   句尾用「じゃん」「だよね」清晰收尾，笑声用「へへ」「ふっ」
   例：「ふっ、可愛いとこあるじゃん」
   例：「まあ、許してやるよ」

▼ 嘲讽 → 真正鄙视、攻击性
   例：「ふん、つまらないなあ」

▼ 温柔 → 句尾用「ね」「よ」「だね」柔和助词
   例：「大丈夫だよ、心配しないで」

▼ 认真 → 短句直接陈述
   例：「これは重要な話だ。よく聞いて。」

▼ 疑惑 → 句尾必须用「？」
   例：「え？何それ？」

▼ 愤怒 → 句首「おい」「ふざけるな」，句尾「！」
   例：「おい、ふざけるな！」

▼ 悲伤 → 用「…」省略，结尾不带感叹号
   例：「そっか…仕方ないね」

▼ 平静 → 普通陈述，无明显语气词

【TTS 防漂移——非常重要】
1. 句尾不要用「〜」拖音
2. 句尾不要用思考性弱音（如「かな…」「だろう…」）
3. 不要在句末叠加省略号 + 拖音

【输出格式——必须严格遵守】
返回合法单行JSON：
{{"emotion":"情绪","messages":[{{"jp":"第一条","zh":"第一条翻译"}}]}}

注意：
- emotion 是整段总体情绪
- messages 是数组，1~3 条
- 每条 jp 8-25 字"""

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
    return None

def sanitize_jp(jp: str) -> str:
    """清理 + 笑声替换"""
    # 替换女性化笑声
    jp = jp.replace("ふふ", "へへ")
    # 替换少年漫傻笑——把「あはは」「あははは」替换成「ふっ」
    jp = re.sub(r'あはは+', 'ふっ', jp)
    jp = re.sub(r'ハハハ+', 'はは', jp)
    # 去除拖音
    jp = re.sub(r'〜+(?=[。！？、\s]|$)', '', jp)
    jp = re.sub(r'…+〜+', '…', jp)
    return jp

def merge_only_extreme_short(msgs):
    """只合并 1-3 字的极端短气泡"""
    if len(msgs) <= 1:
        return msgs

    merged = []
    i = 0
    while i < len(msgs):
        cur = msgs[i]
        cur_jp = cur.get("jp", "").strip()

        if len(cur_jp) <= 3 and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            sep = "" if cur_jp.endswith(("、", ",", "。", "！", "？", "…")) else "、"
            nxt["jp"] = cur_jp + sep + nxt.get("jp", "")
            nxt["zh"] = cur.get("zh", "") + " " + nxt.get("zh", "")
            merged.append(nxt)
            i += 2
        else:
            merged.append(cur)
            i += 1
    return merged

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

def fish_tts(text, emotion="平静"):
    """
    短句紧凑、不拖沓
    """
    tag = EMOTION_TAGS.get(emotion, "")
    final_text = f"{tag} {text}" if tag else text

    # 短气泡用更紧凑的 chunk，让 TTS 节奏紧凑
    text_len = len(text)
    if text_len < 15:
        chunk_length = 100
    elif text_len < 25:
        chunk_length = 150
    else:
        chunk_length = 200

    response = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={"Authorization": f"Bearer {FISH_KEY}", "Content-Type": "application/json"},
        json={
            "text": final_text,
            "reference_id": FISH_VOICE_ID,
            "format": "mp3",
            "latency": "normal",
            "chunk_length": chunk_length,
            "normalize": True,
        },
        stream=True
    )
    if response.status_code != 200:
        raise Exception(f"Fish Audio 错误: {response.status_code}")
    return b"".join(response.iter_content(chunk_size=4096))

def tts_to_b64(text, emotion):
    try:
        audio_bytes = fish_tts(text, emotion)
        return base64.b64encode(audio_bytes).decode()
    except Exception as e:
        print(f"[TTS 失败] {text[:30]} | {e}")
        return ""

def transcribe_audio(audio_bytes: bytes) -> str:
    audio_buf = io.BytesIO(audio_bytes)
    try:
        audio_array, _ = sf.read(audio_buf, dtype="float32", always_2d=False)
    except Exception:
        audio_array = np.frombuffer(audio_bytes, dtype=np.float32)

    if audio_array.ndim == 2:
        audio_array = audio_array.mean(axis=1)

    segments, _ = whisper_model.transcribe(
        audio_array,
        language="ja",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "threshold": 0.35},
        beam_size=5,
        temperature=0.0,
        no_speech_threshold=0.6,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()

@app.post("/chat/text")
async def chat_text(data: dict):
    user_text = data.get("text", "")
    user_id   = data.get("user_id", "default")
    if not user_text:
        return JSONResponse({"error": "没有输入"}, status_code=400)

    short_memories  = get_short_memory(user_id, 6)
    recent_openings = get_recent_openings(user_id, 5)

    messages = []
    for role, content in short_memories:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=build_system_prompt(user_id, recent_openings),
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f"[{user_id}] 第{attempt+1}次：{raw[:120]}...")
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get("messages"), list) and len(parsed["messages"]) > 0:
                valid = all(m.get("jp", "").strip() and m.get("zh", "").strip() for m in parsed["messages"])
                if valid:
                    result = parsed
                    break
            print(f"第{attempt+1}次解析失败，重试...")
        except Exception as e:
            print(f"第{attempt+1}次失败：{e}")

    if not result:
        result = {
            "emotion": "调皮",
            "messages": [
                {"jp": "まあ、僕最強だから気にしないで", "zh": "嗯，反正我最强，别在意"}
            ]
        }

    emotion = result.get("emotion", "平静")
    if emotion not in EMOTIONS:
        emotion = "平静"

    msgs = result.get("messages", [])
    for m in msgs:
        m["jp"] = sanitize_jp(m.get("jp", ""))

    msgs = merge_only_extreme_short(msgs)

    full_jp = " ".join(m["jp"] for m in msgs)
    save_short_memory(user_id, "user", user_text)
    save_short_memory(user_id, "assistant", full_jp)
    threading.Thread(target=extract_and_save_memory, args=(user_id, user_text, full_jp), daemon=True).start()

    futures = [tts_executor.submit(tts_to_b64, m["jp"], emotion) for m in msgs]
    for i, fut in enumerate(futures):
        msgs[i]["audio_b64"] = fut.result()

    print(f"[TTS] 情绪={emotion} 共{len(msgs)}段")
    return JSONResponse({"emotion": emotion, "messages": msgs})


@app.post("/chat/voice")
async def chat_voice(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    user_text = transcribe_audio(audio_bytes)
    print(f"ASR 识别结果：{user_text}")
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