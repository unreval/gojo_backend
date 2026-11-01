"""Static sticker catalog. Shared assets, not per-message chat_media.

Binary objects live in R2 under the manifest prefix. This module only
reads the versioned JSON catalog. Every public function fails closed:
a bad catalog or a storage error means "no sticker", never a request failure.
"""
import json
import os
import re


_ID_RE = re.compile(r'^[a-z0-9_]{1,32}$')
_FILE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$')
_DESC_MAX = 12
_AUDIENCES = ('character', 'user', 'both')

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stickers.json')
_CACHE = None


def _empty():
    return {'version': 0, 'prefix': '', 'items': [], 'by_id': {}}


def _fail(reason):
    print(f'[sticker] {reason}')
    return _empty()


def _audience(raw, sticker_id):
    """Missing `for` is both. An illegal value is both plus one log line."""
    if not isinstance(raw, dict) or 'for' not in raw:
        return 'both'
    value = raw.get('for')
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _AUDIENCES:
            return cleaned
    print(f'[sticker] illegal for={value!r} id={sticker_id}; treating as both')
    return 'both'


def _serves(item, actor):
    audience = item.get('for') if isinstance(item, dict) else None
    if audience not in _AUDIENCES:
        audience = 'both'
    if actor == 'character':
        return audience in ('character', 'both')
    if actor == 'user':
        return audience in ('user', 'both')
    return False


def _load_uncached():
    path = MANIFEST_PATH
    if not os.path.isfile(path):
        return _fail(f'manifest missing path={path}')
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception as exc:
        return _fail(f'manifest unreadable:{type(exc).__name__}')
    if not isinstance(data, dict):
        return _fail('manifest is not an object')
    prefix = data.get('prefix')
    if (not isinstance(prefix, str) or not prefix
            or prefix.startswith(('/', '\\')) or '..' in prefix):
        return _fail('manifest prefix invalid')
    raw_items = data.get('items')
    if not isinstance(raw_items, list):
        return _fail('manifest items invalid')
    accepted = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            print('[sticker] skip non-object item')
            continue
        sticker_id = raw.get('id')
        if not isinstance(sticker_id, str) or not _ID_RE.match(sticker_id):
            print(f'[sticker] skip illegal id={sticker_id!r}')
            continue
        filename = raw.get('file')
        if (not isinstance(filename, str) or not _FILE_RE.match(filename)
                or '/' in filename or '\\' in filename):
            print(f'[sticker] skip illegal file id={sticker_id}')
            continue
        desc = raw.get('desc') if isinstance(raw.get('desc'), str) else ''
        if len(desc) > _DESC_MAX:
            print(f'[sticker] skip desc too long id={sticker_id}')
            continue
        if sticker_id in seen:
            return _fail(f'duplicate id={sticker_id}; manifest discarded')
        seen.add(sticker_id)
        emotion = raw.get('emotion') if isinstance(raw.get('emotion'), str) else ''
        mime = raw.get('mime') if isinstance(raw.get('mime'), str) and raw.get('mime') else 'image/png'
        accepted.append({
            'id': sticker_id,
            'file': filename,
            'mime': mime,
            'emotion': emotion,
            'desc': desc,
            'prefix': prefix,
            'for': _audience(raw, sticker_id),
        })
    return {
        'version': data.get('version') or 1,
        'prefix': prefix,
        'items': accepted,
        'by_id': {item['id']: item for item in accepted},
    }


def load_manifest():
    """Cached catalog. Parse failure returns an empty catalog and does not raise."""
    global _CACHE
    try:
        if _CACHE is not None:
            return _CACHE
        _CACHE = _load_uncached()
        return _CACHE
    except Exception as exc:
        print(f'[sticker] manifest load failed:{type(exc).__name__}')
        _CACHE = _empty()
        return _CACHE


def is_enabled():
    """Non-empty catalog, configured object storage, and not explicitly disabled."""
    try:
        if os.environ.get('STICKERS_ENABLED', '').strip().lower() == 'false':
            return False
        if not (load_manifest().get('items') or []):
            return False
        import media_storage
        return bool(media_storage.is_configured())
    except Exception as exc:
        print(f'[sticker] enabled check failed:{type(exc).__name__}')
        return False


def catalog_for_prompt():
    """One line per emotion. Empty when stickers are off, so the prompt omits them."""
    try:
        if not is_enabled():
            return ''
        groups = []
        index = {}
        for item in load_manifest().get('items') or []:
            if not _serves(item, 'character'):
                continue
            emotion = item.get('emotion') or '其他'
            if emotion not in index:
                index[emotion] = len(groups)
                groups.append((emotion, []))
            groups[index[emotion]][1].append(
                f"{item['id']}={item.get('desc') or ''}")
        return '\n'.join(
            f'{emotion}：' + ' / '.join(parts) for emotion, parts in groups)
    except Exception as exc:
        print(f'[sticker] catalog failed:{type(exc).__name__}')
        return ''


def list_for_user():
    """Entries the user picker can send. User-only rows stay out of the prompt."""
    try:
        items = load_manifest().get('items') or []
        return [dict(item) for item in items if _serves(item, 'user')]
    except Exception as exc:
        print(f'[sticker] user list failed:{type(exc).__name__}')
        return []


def usable_by(sticker_id, actor):
    """True when this id may be sent by 'character' or 'user'."""
    try:
        if actor not in ('character', 'user'):
            return False
        item = get(sticker_id)
        if not item:
            return False
        return _serves(item, actor)
    except Exception as exc:
        print(f'[sticker] usable_by failed:{type(exc).__name__}')
        return False


def get(sticker_id):
    try:
        item = load_manifest().get('by_id', {}).get(str(sticker_id or ''))
        return dict(item) if isinstance(item, dict) else None
    except Exception as exc:
        print(f'[sticker] get failed:{type(exc).__name__}')
        return None


def object_key(item):
    try:
        if not isinstance(item, dict):
            return ''
        prefix = str(item.get('prefix') or '')
        filename = str(item.get('file') or '')
        if not prefix or not filename:
            return ''
        return prefix + filename
    except Exception:
        return ''


def signed_url(sticker_id):
    """Mint a read URL. Storage errors return None and do not raise."""
    try:
        item = get(sticker_id)
        key = object_key(item)
        if not key:
            return None
        import media_storage
        return media_storage.signed_get_url(key)
    except Exception as exc:
        print(f'[sticker] signed url failed id={sticker_id}:{type(exc).__name__}')
        return None
