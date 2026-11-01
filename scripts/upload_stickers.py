"""Upload a local sticker directory to R2 and refresh stickers.json.

Not on the request path. Existing desc/emotion values are kept.
New rows leave desc empty for a person to fill in. --dry-run prints the
plan and does not upload or write the manifest.
"""
import argparse
import json
import mimetypes
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import sticker_registry  # noqa: E402


_IMAGE_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
}


def _manifest_path(path):
    return path or os.path.join(BACKEND, 'stickers.json')


def _load(path):
    if not os.path.isfile(path):
        return {'version': 1, 'prefix': 'stickers/v1/', 'items': []}
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit('stickers.json is not an object')
    data.setdefault('version', 1)
    data.setdefault('prefix', 'stickers/v1/')
    if not isinstance(data.get('items'), list):
        data['items'] = []
    return data


def _mime(filename):
    ext = os.path.splitext(filename)[1].lower()
    return _IMAGE_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or ''


def plan(directory, manifest):
    prefix = str(manifest.get('prefix') or 'stickers/v1/')
    existing = {}
    for item in manifest.get('items') or []:
        if isinstance(item, dict) and item.get('id'):
            existing[item['id']] = item
    rows = []
    skipped = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        mime = _mime(name)
        if mime not in _IMAGE_TYPES.values():
            skipped.append(name)
            continue
        sticker_id = os.path.splitext(name)[0]
        if not sticker_registry._ID_RE.match(sticker_id):
            skipped.append(name)
            continue
        previous = existing.get(sticker_id) or {}
        rows.append({
            'id': sticker_id,
            'file': name,
            'mime': mime,
            'emotion': previous.get('emotion') or '',
            'desc': previous.get('desc') or '',
            'object_key': prefix + name,
            'path': path,
        })
    return rows, skipped


def main():
    parser = argparse.ArgumentParser(description='Upload sticker files and refresh the manifest skeleton.')
    parser.add_argument('--dir', required=True, help='Local directory of sticker images')
    parser.add_argument('--manifest', default='', help='Defaults to gojo_backend/stickers.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    directory = os.path.abspath(args.dir)
    if not os.path.isdir(directory):
        raise SystemExit(f'not a directory: {directory}')
    manifest_path = _manifest_path(args.manifest)
    manifest = _load(manifest_path)
    rows, skipped = plan(directory, manifest)
    for name in skipped:
        print(f'[sticker-upload] skip {name}')
    if args.dry_run:
        for row in rows:
            print(f"[sticker-upload] dry-run {row['path']} -> {row['object_key']}")
        print(f'[sticker-upload] dry-run would write {len(rows)} items to {manifest_path}')
        return 0
    import media_storage
    for row in rows:
        with open(row['path'], 'rb') as handle:
            media_storage.put_bytes(row['object_key'], handle.read(), row['mime'])
        print(f"[sticker-upload] put {row['object_key']}")
    manifest['items'] = [
        {key: row[key] for key in ('id', 'file', 'mime', 'emotion', 'desc')}
        for row in rows
    ]
    with open(manifest_path, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(f'[sticker-upload] wrote {len(rows)} items to {manifest_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
