"""Canvas file inventory and bounded, authenticated downloads."""
from __future__ import annotations
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from plugins.icampus.icampus import BASE, Client, CollectionError

MAX_BYTES = 200_000_000  # Use decimal MB, the conservative interpretation of 200MB.
LOG = logging.getLogger(__name__)


def file_key(meta):
    return f'{meta["course_id"]}:{meta["file_id"]}'


def iter_new_files(collected, state) -> tuple[list[dict], list[dict]]:
    """Return new files and safe diagnostics; isolate malformed inventory rows."""
    if not isinstance(collected, dict):
        raise ValueError('Canvas 수집 결과는 객체여야 함')
    courses = collected.get('courses', [])
    names = {str(c['id']): str(c.get('name') or c['id'])
             for c in (courses if isinstance(courses, list) else [])
             if isinstance(c, dict) and 'id' in c}
    seen = set(state.get('seen', {}))
    seen.update(file_key(x) for x in state.get('uploaded', {}).values())
    result, skipped = [], []

    def skip(key, reason):
        skipped.append({'key': key, 'reason': reason})
        # Raw records/URLs may contain credentials; log only our own diagnosis.
        LOG.warning('NotebookLM 수집 항목 건너뜀: %s', reason)

    items = collected.get('items', [])
    if not isinstance(items, list):
        skip('items', 'Canvas 파일 목록 형식 오류')
        return result, skipped
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            skip(f'item:{index}', 'Canvas 파일 항목은 객체여야 함')
            continue
        source = 'canvas'
        module_item_id = None
        if item.get('kind') == 'learning':
            if item.get('content_type') not in ('pdf', 'file') or item.get('locked'):
                continue
            content = item.get('content') or {}
            if not isinstance(content, dict):
                skip(f'item:{index}', 'LearningX 파일 메타데이터 형식 오류')
                continue
            module_item_id = str(item.get('id', '')).rsplit(':', 1)[-1]
            if not module_item_id.isdecimal():
                skip(f'item:{index}', 'LearningX 자료 식별자 형식 오류')
                continue
            source = 'learningx'
            item = {**item, 'file_id': 'learningx-' + module_item_id,
                    'filename': content.get('file_name') or item.get('title'),
                    'size_hint': content.get('total_file_size'),
                    'url': content.get('view_url') or item.get('url')}
        elif item.get('kind') != 'file':
            continue
        cid = str(item.get('course_id') or '')
        fid = item.get('file_id')
        key = f'{cid or "?"}:{fid if fid is not None else item.get("id") or "?"}'
        try:
            if fid is None:
                match = re.search(r'/files/(\d+)(?:/|$)', urlsplit(item.get('url') or '').path)
                fid = match[1] if match else str(item.get('id', '')).rsplit(':', 1)[-1]
            key = f'{cid or "?"}:{fid or "?"}'
            valid_fid = bool(re.fullmatch(r'learningx-\d+', str(fid))) if source == 'learningx' else str(fid).isdecimal()
            if not cid.isdecimal() or not valid_fid:
                skip(key, 'Canvas 파일 식별자 형식 오류')
                continue
            row = {'course_id': cid, 'course_name': names.get(cid, str(item.get('course_name') or cid)),
                   'file_id': str(fid), 'title': str(item.get('title') or ''),
                   'filename': str(item.get('filename') or item.get('title') or ''),
                   'url': str(item.get('url') or ''),
                   'week': str(item.get('week') or item.get('module') or ''),
                   'size_hint': item.get('size_hint', item.get('size'))}
        except Exception as exc:
            skip(key, 'Canvas 파일 메타데이터 형식 오류 (' + type(exc).__name__ + ')')
            continue
        row['source'] = source
        if source == 'learningx':
            row['module_item_id'] = module_item_id
        if file_key(row) not in seen:
            result.append(row)
            seen.add(file_key(row))
    return result, skipped


def safe_filename(value):
    name = re.split(r'[/\\]', str(value))[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '_', name).strip().strip('.')
    if not name:
        name = 'lecture'
    # Bound UTF-8 bytes for readable download and deterministic upload names.
    suffix = Path(name).suffix[:12]
    stem = name[:-len(suffix)] if suffix else name
    while len((stem + suffix).encode('utf-8')) > 150:
        stem = stem[:-1]
    return stem + suffix


def _download_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise CollectionError('안전하지 않은 파일 다운로드 URL')
    return value


def _learningx_download_url(meta, auth):
    """Resolve the authenticated file response via its Canvas module viewer.

    LCMS's bare view URL is access-restricted. The Canvas module link provides
    the course authorization; signed file URLs stay in memory for this download.
    """
    if not isinstance(auth, tuple) or len(auth) < 3:
        raise CollectionError('LearningX 브라우저 인증 필요')
    context, page, session = auth[:3]
    cid, mid = str(meta['course_id']), str(meta['module_item_id'])
    if not cid.isdecimal() or not mid.isdecimal():
        raise CollectionError('LearningX 자료 식별자 형식 오류')
    deadline = min(meta.get('_deadline', time.monotonic() + 90), time.monotonic() + 90)
    targets = []

    def response_file(response):
        if response.status not in (200, 206) or response.request.method != 'GET':
            return
        media = response.headers.get('content-type', '').split(';')[0].lower()
        parsed = urlsplit(response.url)
        if parsed.hostname not in ('lcms.skku.edu', 'canvas.skku.edu'):
            return
        if (media in ('application/pdf', 'application/msword',
                      'application/vnd.ms-excel', 'application/vnd.ms-powerpoint')
                or media.startswith('application/vnd.openxmlformats-officedocument.')
                or Path(parsed.path).suffix.lower() in ('.pdf', '.ppt', '.pptx', '.doc', '.docx', '.txt', '.md')):
            targets.append(response.url)

    def download_file(event):
        parsed = urlsplit(event.url)
        if parsed.hostname in ('lcms.skku.edu', 'canvas.skku.edu'):
            # Keep the browser's authenticated response. Re-fetching this URL
            # can lose request authorization and return an HTML error page.
            meta['_browser_download'] = event
            targets.append(event.url)
        else:
            event.cancel()

    page.on('response', response_file)
    page.on('download', download_file)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError('LearningX 파일 시간 제한 초과')
        page.goto(f'{BASE}/courses/{cid}/modules/items/{mid}',
                  wait_until='domcontentloaded', timeout=int(min(45, remaining) * 1000))
        clicked_frames = set()
        while not targets and time.monotonic() < deadline:
            # LearningX Office/archive files do not auto-load like PDFs. Their
            # viewer exposes a clickable filename inside its iframe.
            for frame in page.frames:
                if frame in clicked_frames:
                    continue
                control = frame.locator('.xnbc-file-name')
                if control.count() and control.first.is_visible():
                    clicked_frames.add(frame)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    control.first.click(timeout=int(min(5, remaining) * 1000))
                    if targets:
                        break
            page.wait_for_timeout(250)
        if not targets:
            raise CollectionError('LearningX PDF/파일 응답 미확인')
        for cookie in context.cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
        return _download_url(targets[0])
    except CollectionError:
        raise
    except Exception as exc:
        raise CollectionError('LearningX 파일 조회 실패: ' + type(exc).__name__) from None
    finally:
        page.remove_listener('response', response_file)
        page.remove_listener('download', download_file)


def _save_browser_download(event, meta, dest_dir, deadline):
    meta['filename'] = safe_filename(event.suggested_filename or meta['filename'])
    folder = Path(dest_dir) / hashlib.sha256(file_key(meta).encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            temp = Path(stream.name)
        os.chmod(temp, 0o600)
        event.save_as(str(temp))
        digest, size = hashlib.sha256(), 0
        with temp.open('rb') as stream:
            while chunk := stream.read(64 * 1024):
                if time.monotonic() >= deadline:
                    raise CollectionError('파일 다운로드 시간 제한 초과')
                digest.update(chunk)
                size += len(chunk)
        if not size:
            raise CollectionError('빈 파일 수신')
        destination = folder / meta['filename']
        os.replace(temp, destination)
        temp = None
        meta.update({'sha256': digest.hexdigest(), 'size': size, 'path': str(destination)})
        return destination
    except CollectionError:
        raise
    except Exception as exc:
        raise CollectionError('브라우저 파일 다운로드 실패: ' + type(exc).__name__) from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def download(meta, auth, dest_dir) -> Path:
    """Use the authenticated browser download or its requests cookie session.

    Mutate metadata with the authoritative filename, size, digest and local path.
    Signed URLs and cookies remain in memory and are never stored or logged.
    """
    session = auth[2] if isinstance(auth, tuple) else auth
    deadline = meta.get('_deadline', time.monotonic() + 120)
    cid, fid = str(meta['course_id']), str(meta['file_id'])
    if not cid.isdecimal():
        raise CollectionError('Canvas 과목 식별자 형식 오류')
    if meta.get('source') == 'learningx':
        if not re.fullmatch(r'learningx-\d+', fid):
            raise CollectionError('LearningX 자료 식별자 형식 오류')
        info = {}
        target = _learningx_download_url(meta, auth)
        event = meta.pop('_browser_download', None)
        if event is not None:
            return _save_browser_download(event, meta, dest_dir, deadline)
    else:
        if not fid.isdecimal():
            raise CollectionError('Canvas 파일 식별자 형식 오류')
        try:
            info, _ = Client(session, deadline).get(f'/api/v1/courses/{cid}/files/{fid}')
            if not isinstance(info, dict):
                raise CollectionError('Canvas 파일 메타데이터 형식 오류')
        except CollectionError:
            info = {}
        target = info.get('url') or f'{BASE}/courses/{cid}/files/{fid}/download'
    if info.get('locked_for_user'):
        raise CollectionError('접근 제한된 Canvas 파일')
    meta['filename'] = safe_filename(info.get('filename') or info.get('display_name') or meta['filename'])
    meta['size_hint'] = info.get('size', meta.get('size_hint'))
    if time.monotonic() >= deadline:
        raise CollectionError('파일 다운로드 시간 제한 초과')
    temp = None
    try:
        response = session.get(_download_url(target), stream=True, timeout=(10, 30))
        with response:
            if response.status_code != 200:
                raise CollectionError('파일 다운로드 HTTP ' + str(response.status_code))
            _download_url(response.url)
            if urlsplit(response.url).hostname in ('accounts.google.com', 'icampus.skku.edu'):
                raise CollectionError('iCampus 로그인 세션 만료')
            media = response.headers.get('Content-Type', '').split(';')[0].lower()
            if media in ('text/html', 'application/json'):
                raise CollectionError('파일 대신 로그인/오류 페이지 수신')
            if not info:
                disposition = response.headers.get('Content-Disposition', '')
                encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
                plain = re.search(r'filename="([^\"]+)"|filename=([^;]+)', disposition, re.I)
                if encoded or plain:
                    meta['filename'] = safe_filename(unquote(encoded[1]) if encoded else (plain[1] or plain[2]))
            folder = Path(dest_dir) / hashlib.sha256(file_key(meta).encode()).hexdigest()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
                temp = Path(stream.name)
                os.chmod(temp, 0o600)
                for chunk in response.iter_content(64 * 1024):
                    if not size and chunk.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                        raise CollectionError('파일 대신 HTML 로그인/오류 페이지 수신')
                    if time.monotonic() >= deadline:
                        raise CollectionError('파일 다운로드 시간 제한 초과')
                    size += len(chunk)
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not size:
                raise CollectionError('빈 파일 수신')
            destination = folder / meta['filename']
            os.replace(temp, destination)
            temp = None
            meta.update({'sha256': digest.hexdigest(), 'size': size, 'path': str(destination)})
            return destination
    except requests.RequestException as exc:
        raise CollectionError('파일 다운로드 실패: ' + type(exc).__name__) from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
