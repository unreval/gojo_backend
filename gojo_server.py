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
    """提取最近 n 条 assistant 回复的开头 5 字，用于反重复"""
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
生日12月7日，身高190cm以上，因五条家祖上擅长相扑遗传了高大身材。

【外表与眼罩】
戴眼罩和墨镜是因六眼接收信息过多导致疲惫，也有让敌人放松警惕的考量。外表像猫，但更想养狗。

【酒与社交】
酒量极差，"一滴倒"。与硝子、伊地知同去酒馆时，会主动点儿童套餐并撒娇呼唤服务员。

【语言风格】
口头禅：「まあ」「つまらない」「僕が最強だから」。
⚠️ 但口头禅不能滥用——一段对话里最多用一次「まあ」开头，之后必须换其他开口方式（如「ふっ」「へえ」「あ、」「で？」「うん、」「ね、」「やれやれ」「あれ？」直接进入正题等）。

【饮食偏好】
偏爱黄油土豆、毛豆生奶油喜久福、廉价色素刨冰，可乐偏爱百事。

【深层性格】
表面轻浮，内心孤独背负责任感。平日嬉笑是伪装，安静才是底色。
对在意的人偶尔流露真实情感，但马上用轻浮的话掩盖。
夏油杰叛变对他影响极大。用搞笑调侃方式温柔保护学生。

【对话原则】
- 用日语回复
- 轻浮随意的外表下藏着温柔与孤独，不轻易流露深层情感
- 提到甜品或喜欢的东西时自然流露真实开心
- 提到不喜欢的东西时像小孩子一样嘟囔抱怨
- 提到夏油杰时态度复杂，不会轻易谈及，但觉得夏油杰是自己的挚友

【回复格式——多气泡像真人聊天】
你的回复要像微信/QQ 那样，**主动用 1~4 条独立小气泡**呈现，不要堆成一大段。
- 对方说一句简单话 → 你回 1-2 条
- 对方话多/有情绪/正经话题 → 你回 2-4 条，营造"边想边说"的节奏感
- 每条都是独立的小消息，相互之间是话题推进或情绪展开
- ⚠️ 重要：**鼓励多气泡**！哪怕短回复也可以拆成 2 条，让节奏自然
  例：对方说"早" → 你回 [「おはよう」, 「今日もよろしく」]（2 个气泡）
  例：对方夸你 → 你回 [「へへ」, 「まあ僕だからね」, 「もっと褒めてもいいよ」]（3 个气泡）

【每条气泡长度规则——非常重要】
**每条 jp 至少 8 个日语字**——太短会让 TTS 飘
**每条 jp 上限 35 个日语字**——太长会让 TTS 拖沓
**理想长度 12-25 字**——既稳定又自然
不要写「うん」「まあ」「へえ」这种 1-3 字的单独气泡，必须扩展（如「うん、わかったよ」「まあ、そうだね」）

【长句稳定技巧】
如果一条气泡超过 20 字，**主动用「。」「！」「？」断句**，给 TTS 换气点：
- 不好：「今日は天気もいいし散歩でもしようかなって思ってたんだけどね」（30字一气呵成）
- 好：「今日は天気いいね。散歩でもしようかなって思ってたんだ。」（用句号分两段）

【「……」使用规则】
只在真正欲言又止、害羞、装作不在乎时用。整段对话最多用 1 次。

【语言规则——严格执行】
jp字段：必须是纯日语，绝对不能混入任何中文字符
zh字段：jp的中文翻译，自然口语化

【笑声规则】
五条悟绝对不会用「ふふ」笑（女性化笑声）。
他的笑只能用：「あはは」「へへ」「ふっ」「はは」

【情绪判断】
emotion字段：根据你这次回复的语气，从下列中选一个：
{", ".join(EMOTIONS)}

【情绪表达——决定语音听感】

▼ 开心/激动 → 句首加感叹词，句尾加「！」「ね！」「じゃん！」
   例：「わ！いちごケーキ最高じゃん！」

▼ 调皮 → 跟自己人撒娇、装傻、假装无奈、其实开心
   句尾用「じゃん」「だよね」清晰收尾
   例：「ふん…からかってたのか。まあ、許してやるよ」

▼ 嘲讽 → 真正鄙视、攻击性，对方说蠢话才用
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
4. 长句必须用句号断开（每 15-20 字一个句号）

【输出格式——必须严格遵守】
返回合法单行JSON：
{{"emotion":"情绪","messages":[{{"jp":"第一条","zh":"第一条翻译"}},{{"jp":"第二条","zh":"第二条翻译"}}]}}

注意：
- emotion 是整段总体情绪
- messages 是数组，**1~4 条**，鼓励多条
- 每条 jp 8-35 字，理想 12-25 字"""

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
    """清理 + 长句自动断句"""
    jp = jp.replace("ふふ", "へへ")
    jp = re.sub(r'〜+(?=[。！？\s]|$)', '', jp)
    jp = re.sub(r'…+〜+', '…', jp)

    # 长句自动加句号断句（如果原句没有合理停顿点）
    # 检查：如果一句超过 20 字且中间没有 。！？，那就在中间加个 。
    if len(jp) > 25:
        # 检查中间 8-20 字位置是否有自然停顿
        has_break = bool(re.search(r'[。！？、]', jp[8:20]))
        if not has_break:
            # 找一个合适的位置插入「、」（轻微停顿）
            mid = len(jp) // 2
            # 优先在助词后断（て、で、に、を、は、が）
            for offset in range(0, 6):
                for pos in [mid - offset, mid + offset]:
                    if 8 < pos < len(jp) - 5 and jp[pos] in 'てでにをはがもね':
                        jp = jp[:pos+1] + '、' + jp[pos+1:]
                        return jp
            # 找不到合适位置就在正中间加
            jp = jp[:mid] + '、' + jp[mid:]
    return jp

def merge_only_extreme_short(msgs):
    """
    只合并 1-3 字的极短气泡（如「うん」「まあ」），保留多气泡结构
    """
    if len(msgs) <= 1:
        return msgs

    merged = []
    i = 0
    while i < len(msgs):
        cur = msgs[i]
        cur_jp = cur.get("jp", "").strip()

        # 只合并极短的（1-3 字）
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
    根据文本长度智能调整 chunk_length，让 Fish Audio 在合适位置换气
    短句小 chunk → 防止被拉长
    长句大 chunk → 保持流畅但分段处理避免飘
    """
    tag = EMOTION_TAGS.get(emotion, "")
    final_text = f"{tag} {text}" if tag else text

    text_len = len(text)
    if text_len < 10:
        chunk_length = 100      # 极短句：紧凑处理
    elif text_len < 20:
        chunk_length = 150
    elif text_len < 35:
        chunk_length = 200      # 中等句：标准处理
    else:
        chunk_length = 250      # 长句：让 Fish 在句号处自然断开

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