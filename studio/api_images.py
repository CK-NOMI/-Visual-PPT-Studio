"""Lossless transport deduplication using DeepSeek's expiring Files API.

The cache stores opaque file references only, scoped by endpoint and credential
digest. Images, user filenames and credentials are never written to telemetry.
"""
import base64
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse

import httpx

from . import core
from .api_limits import current

_lock = threading.Lock()
_unsupported = set()


class ImageUploadError(RuntimeError):
    pass


def file_reference_failed(response):
    if response.status_code not in {400, 404}:
        return False
    text = response.text.lower()
    return ('file' in text and any(term in text for term in
        ('not_found', 'not found', 'expired', 'invalid_file_id')))


def invalidate_references(messages, *, base_url, api_key):
    api_key=api_key or ''
    ids = {part.get('file_id') for message in messages
           if isinstance(message.get('content'), list)
           for part in message['content'] if isinstance(part, dict) and part.get('type') == 'file'}
    if not ids:
        return
    scope = hashlib.sha256((base_url + '\0' + api_key).encode()).hexdigest()
    root = Path(core.DATA) / 'api_image_cache'
    root.mkdir(parents=True, exist_ok=True)
    import fcntl
    with _lock, (root / (scope + '.lock')).open('a') as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            path = root / (scope + '.json')
            try:cache = json.loads(path.read_text())
            except (OSError, ValueError):return
            cache = {k: v for k, v in cache.items() if v.get('file_id') not in ids}
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(cache));temp.chmod(0o600);temp.replace(path)
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def _log(**values):
    from .api_usage import _append, context
    from datetime import datetime, timezone
    ctx = context()
    _append('transfers.jsonl', {**{k: ctx.get(k) for k in
        ('task_id', 'project_id', 'slide_id', 'module')},
        'timestamp': datetime.now(timezone.utc).isoformat(), **values})


def prepare_messages(messages, *, base_url, api_key, cancel_event=None):
    """Replace inline image blocks by identical file references when supported."""
    api_key=api_key or ''
    # Custom OpenAI-compatible providers keep their original transport unless
    # explicitly opted in; do not upload private files to a guessed endpoint.
    setting = os.environ.get('PPT_STUDIO_IMAGE_REFERENCES', 'auto').lower()
    enabled = setting in {'1', 'true', 'on'} or (
        setting == 'auto' and urlparse(base_url).hostname == 'api.deepseek.com')
    if not enabled:
        return messages
    scope = hashlib.sha256((base_url + '\0' + api_key).encode()).hexdigest()
    if scope in _unsupported:
        return messages
    output = deepcopy(messages)
    blocks = []
    for message in output:
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'image_url':
                continue
            value = part.get('image_url') or {}
            url = value.get('url') if isinstance(value, dict) else value
            # Explicit low/detail conversions must retain their original meaning.
            if isinstance(value, dict) and value.get('detail') == 'low':
                continue
            if isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url:
                blocks.append((part, url))
    if not blocks:
        return messages
    root = Path(core.DATA) / 'api_image_cache'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_path = root / (scope + '.json')
    # Synchronize both threads and Studio processes sharing the same workspace.
    import fcntl
    with _lock, (root / (scope + '.lock')).open('a') as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            try:
                cache = json.loads(cache_path.read_text())
            except (OSError, ValueError):
                cache = {}
            if not isinstance(cache,dict):cache={}
            cache = {k: v for k, v in cache.items() if isinstance(v, dict)
                     and isinstance(v.get('valid_until'),(int,float))
                     and isinstance(v.get('file_id'),str) and v['valid_until'] > time.time()}
            for part, url in blocks:
                if cancel_event is not None and cancel_event.is_set():
                    raise ImageUploadError('任务已取消，停止后续图片上传。')
                try:
                    header, encoded = url.split(';base64,', 1)
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    # Leave malformed input to the existing provider validation.
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                found = cache.get(digest)
                reused = found is not None
                if not found:
                    budget = current()
                    if budget:
                        budget.before_upload(len(raw) + 1024)
                    mime = header[5:]
                    extension = {'image/png': 'png', 'image/jpeg': 'jpg',
                                 'image/webp': 'webp', 'image/gif': 'gif'}.get(mime, 'img')
                    started = time.perf_counter()
                    try:
                        with httpx.Client(timeout=30) as client:
                            response = client.post(base_url.rstrip('/') + '/files',
                                headers={'Authorization': 'Bearer ' + api_key},
                                data={'purpose': 'user_data', 'expires_after[anchor]': 'created_at',
                                      'expires_after[seconds]': '86400'},
                                files={'file': ('image.' + extension, raw, mime)})
                        _log(kind='upload', image_sha256=digest, upload_bytes=len(raw),
                             http_status=response.status_code,
                             duration_ms=round((time.perf_counter() - started) * 1000, 1))
                        if response.status_code in {404, 405, 415, 501}:
                            _unsupported.add(scope)
                            _log(kind='inline_fallback', reason='files_api_unsupported', upload_bytes=0)
                            return messages
                        if response.status_code >= 400:
                            if budget and response.status_code in {401, 402, 403}:
                                budget.paused_reason = f'DeepSeek 图片上传 HTTP {response.status_code}，停止后续请求'
                            raise ImageUploadError(f'DeepSeek 图片上传失败（HTTP {response.status_code}）；已停止，未重复上传。')
                        try:info = response.json()
                        except ValueError as exc:
                            raise ImageUploadError('DeepSeek 图片上传返回无效 JSON，已停止后续调用。') from exc
                        if not isinstance(info,dict) or not isinstance(info.get('id'), str) or not info['id'].startswith('file-'):
                            raise ImageUploadError('DeepSeek 图片上传未返回有效 file_id；未继续模型调用。')
                    except httpx.HTTPError as exc:
                        _log(kind='upload_failed', image_sha256=digest, upload_bytes=len(raw),
                             error_class=type(exc).__name__)
                        raise ImageUploadError('DeepSeek 图片上传网络失败；已停止，避免自动重复传输。') from exc
                    found = {'file_id': info['id'], 'valid_until': min(
                        float(info.get('expires_at') or (time.time() + 86400)), time.time() + 86400) - 300}
                    cache[digest] = found
                    temp = cache_path.with_suffix('.tmp')
                    temp.write_text(json.dumps(cache))
                    temp.chmod(0o600)
                    temp.replace(cache_path)
                part.clear()
                part.update({'type': 'file', 'file_id': found['file_id']})
                _log(kind='image_reference', image_sha256=digest, reused=reused,
                     inline_bytes_avoided=len(url.encode()), upload_bytes=0)
            return output
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


async def prepare_messages_async(messages, *, base_url, api_key):
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(prepare_messages, messages,
            base_url=base_url, api_key=api_key, cancel_event=cancelled)
    except asyncio.CancelledError:
        cancelled.set()
        raise
