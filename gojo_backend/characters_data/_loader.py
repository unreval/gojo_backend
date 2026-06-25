"""通用角色加载器：从 characters_data/<id>/ 读取人设资料。
所有角色文件夹必须有相同的接口：
  - core.py:        CORE_PROMPT, GREETING, VOICE_ID, NAME, NAME_EN
  - memories.py:    SEED_MEMORIES  (list of tuple: content, category, keywords, importance)
  - canon_lock.py:  CANON_LOCK  (str)
  - lore.json:      可选；剧情/关系/世界观背景，带时间档
"""
import os
import json
import importlib

# 角色文件夹的绝对路径
_DATA_DIR = os.path.dirname(__file__)

# JSON lore 缓存（启动后只读一次硬盘）
_lore_cache = {}


def load_core(character_id: str) -> dict:
    """加载角色核心：返回 {core_prompt, greeting, voice_id, name, name_en}。
    没找到模块返回 None。"""
    try:
        mod = importlib.import_module(f'characters_data.{character_id}.core')
        return {
            'core_prompt': getattr(mod, 'CORE_PROMPT', ''),
            'greeting':    getattr(mod, 'GREETING', ''),
            'voice_id':    getattr(mod, 'VOICE_ID', ''),
            'name':        getattr(mod, 'NAME', character_id),
            'name_en':     getattr(mod, 'NAME_EN', ''),
        }
    except Exception as e:
        print(f'[loader] 加载 {character_id}/core.py 失败：{e}')
        return None


def load_memories(character_id: str) -> list:
    """加载角色预置背景记忆，list of (content, category, keywords, importance)。
    没找到返回 []。"""
    try:
        mod = importlib.import_module(f'characters_data.{character_id}.memories')
        return getattr(mod, 'SEED_MEMORIES', [])
    except Exception as e:
        print(f'[loader] 加载 {character_id}/memories.py 失败：{e}')
        return []


def load_canon_lock(character_id: str) -> str:
    """加载角色铁律。没找到返回空字符串（不影响整体 prompt）。"""
    try:
        mod = importlib.import_module(f'characters_data.{character_id}.canon_lock')
        return getattr(mod, 'CANON_LOCK', '')
    except Exception as e:
        print(f'[loader] 加载 {character_id}/canon_lock.py 失败：{e}')
        return ''


def load_lore(character_id: str) -> dict:
    """加载角色剧情 lore.json，带缓存。
    没找到返回空结构（不会让检索报错）。"""
    if character_id in _lore_cache:
        return _lore_cache[character_id]
    path = os.path.join(_DATA_DIR, character_id, 'lore.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'[loader] 加载 {character_id}/lore.json 失败（没有就忽略）：{e}')
        data = {'_说明': {}, '条目': []}
    _lore_cache[character_id] = data
    return data


def reload_lore(character_id: str):
    """手改完 lore.json 想立刻生效、又不想重启服务时调用一次。"""
    _lore_cache.pop(character_id, None)
    return load_lore(character_id)


def list_registered_characters():
    """返回 REGISTRY 里所有角色 id。"""
    from characters_data import REGISTRY
    return list(REGISTRY)
