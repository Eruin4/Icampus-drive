"""Bounded, failure-isolated iCampus -> per-course Google Drive synchronization."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from plugins.icampus.icampus import PROJECT, TZ, CollectionError, authenticated, clean, load, now, write
from plugins.icampus.nb_files import download, file_key, iter_new_files, safe_filename
from plugins.icampus.nb_drive import DriveError, course_destination, course_labels, move_file, require_rclone, upload_file
from plugins.icampus.nb_notices import plan_notices, write_notices

LOG = logging.getLogger(__name__)


class BudgetExpired(BaseException):
    """A global deadline must escape per-file Exception handlers and retries."""


def _expire(*_):
    raise BudgetExpired()


@contextlib.contextmanager
def time_budget(seconds):
    previous = signal.signal(signal.SIGALRM, _expire)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)


def read_collection(root, existing, deadline):
    before = load(root / 'last-collected.json', {}).get('checked_at')
    if not existing:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise BudgetExpired()
        result = subprocess.run([sys.executable, '-m', 'plugins.icampus.icampus', '--budget', str(min(900, remaining))],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, timeout=max(1, remaining))
        if result.returncode:
            raise RuntimeError('오늘 iCampus 수집 실패')
    data = load(root / 'last-collected.json', {})
    if not existing and data.get('checked_at') == before:
        raise RuntimeError('새 정상 수집 결과 없음')
    try:
        today = datetime.fromisoformat(data['checked_at']).astimezone(TZ).date() == datetime.now(TZ).date()
    except (KeyError, TypeError, ValueError):
        today = False
    if not today or data.get('status') != 'collected':
        raise RuntimeError('오늘 정상 수집 결과 없음: 먼저 iCampus 수집을 실행하세요.')
    if not isinstance(data.get('items'), list) or not isinstance(data.get('courses'), list):
        raise RuntimeError('iCampus 수집 결과 형식 오류')
    return data


def read_state(root):
    state = load(root / 'drive.json')
    if state is None:
        # Keep course metadata, but NotebookLM successes and course selection
        # must not restrict Drive uploads. Leave legacy state untouched.
        legacy = load(root / 'notebooklm.json', {})
        state = {'courses': legacy.get('courses', {}), 'uploaded': {}, 'last_sync': None}
        for key in ('semester',):
            if key in legacy:
                state[key] = legacy[key]
    seen = load(root / 'drive-seen.json', {})
    if not isinstance(state, dict) or not isinstance(state.get('courses'), dict) or not isinstance(state.get('uploaded'), dict):
        raise RuntimeError('Google Drive 상태 형식 오류: 기존 파일 확인 필요')
    if not isinstance(seen, dict):
        raise RuntimeError('Google Drive 처리 이력 형식 오류')
    for digest, row in state['uploaded'].items():
        if not isinstance(digest, str) or len(digest) != 64 or not isinstance(row, dict) or not {'course_id', 'file_id'} <= row.keys():
            raise RuntimeError('Google Drive 업로드 상태 형식 오류')
    # The collector selects current-semester courses. Remove an old allowlist
    # so existing checkpoints and future runs include every collected course.
    state.pop('course_scope', None)
    if not isinstance(state.setdefault('notice_uploads', {}), dict):
        raise RuntimeError('Google Drive 공지 이력 형식 오류')
    return state, seen


def failure(summary, stage, exc, meta=None):
    # Do not publish third-party exception text; it may contain signed URLs.
    reason = str(exc) if isinstance(exc, (CollectionError, DriveError)) else type(exc).__name__
    row = {'stage': stage, 'reason': reason}
    if meta is not None:
        row['key'] = file_key(meta)
    summary['failures'].append(row)
    LOG.warning('Google Drive %s 단계 실패: %s', stage, reason)


def _remember(seen, row, reason):
    seen[file_key(row)] = {'sha256': row.get('sha256'), 'reason': reason, 'at': now()}


def _upload_path(meta, folder):
    """Stable name makes a retry recognizable even after a crash before state save."""
    original = Path(meta['path'])
    name = safe_filename(f'{meta["course_id"]}-{meta["file_id"]}-{original.name}')
    path = Path(folder) / name
    if not path.exists():
        os.link(original, path)
    return path


def _upload_material(row, upload_folder, root, state, seen, summary, deadline):
    cid = str(row['course_id'])
    course_folder = upload_folder / cid
    course_folder.mkdir(mode=0o700, exist_ok=True)
    destination = upload_file(cid, _upload_path(row, course_folder),
                              course_folder, deadline=deadline, course_name=row['drive_folder_name'])
    course = state['courses'].setdefault(cid, {'name': row['course_name']})
    course['drive_folder_name'] = row['drive_folder_name']
    course['drive_destination'] = course_destination(cid, row['drive_folder_name'])
    state['uploaded'][row['sha256']] = {
        'course_id': cid, 'file_id': row['file_id'], 'filename': row['filename'],
        'drive_path': destination, 'uploaded_at': now()}
    # Checkpoint each success; rejected files from older runs remain eligible.
    write(root / 'drive.json', clean(state))
    summary['uploaded'] += 1
    _remember(seen, row, '업로드 완료')
    write(root / 'drive-seen.json', clean(seen))


def _upload_notice(export, upload_folder, root, state, summary, deadline):
    cid = export['course_id']
    course_folder = upload_folder / cid
    course_folder.mkdir(mode=0o700, exist_ok=True)
    path = write_notices(export, course_folder)
    key = f'{cid}:{export["notice_id"]}'
    target = export['drive_destination'] + path.name
    previous = state['notice_uploads'].get(key)
    if previous and previous['drive_path'] != target:
        move_file(previous['drive_path'], target, deadline=deadline)
        # Checkpoint the rename before copying so a failed copy retries in place.
        previous['drive_path'] = target
        write(root / 'drive.json', clean(state))
    destination = upload_file(cid, path, course_folder, deadline=deadline,
                              course_name=export['course_name'])
    course = state['courses'].setdefault(cid, {'name': export['course_name']})
    course['drive_folder_name'] = export['course_name']
    course['drive_destination'] = export['drive_destination']
    state['notice_uploads'][key] = {'drive_path': destination, 'uploaded_at': now()}
    write(root / 'drive.json', clean(state))
    summary['uploaded'] += 1
    summary['notices_uploaded'] += 1


def sync(args, root, deadline):
    state, seen = read_state(root)
    data = read_collection(root, args.existing or args.dry_run, deadline)
    # Earlier runs marked policy rejections as seen. Only upload successes may
    # suppress files now that every available course file goes directly to Drive.
    uploaded_seen = {key: row for key, row in seen.items()
                     if isinstance(row, dict) and row.get('reason') == '업로드 완료'}
    files, skipped = iter_new_files(data, {**state, 'seen': uploaded_seen})
    labels = course_labels(data['courses'], state['courses'])
    for row in files:
        row['drive_folder_name'] = labels.get(row['course_id'], row['course_name'])
    notices = plan_notices(data, labels)
    out_of_scope = 0
    inventory_failures = [{'stage': 'inventory', **item} for item in skipped]
    if args.dry_run:
        for row in files:
            row['drive_destination'] = course_destination(row['course_id'], row['drive_folder_name'])
        print(json.dumps(clean({'dry_run': True, 'checked_at': data['checked_at'], 'new_files': len(files),
                                'candidates': files,
                                'notice_files': notices,
                                'skipped_items': len(skipped), 'out_of_scope': out_of_scope, 'failures': inventory_failures,
                                'pending_steps': ['iCampus 자료 다운로드', 'Google Drive 업로드']}),
                         ensure_ascii=False, indent=2))
        return 0
    summary = {'new_files': len(files), 'downloaded': 0,
               'uploaded': 0, 'notice_files': len(notices), 'notices_uploaded': 0,
               'skipped_items': len(skipped), 'out_of_scope': out_of_scope, 'failures': inventory_failures}
    work_started = not files
    try:
        with tempfile.TemporaryDirectory(prefix='nb-download-', dir=root) as folder:
            upload_folder = Path(folder) / 'uploads'
            upload_folder.mkdir(mode=0o700)
            if files:
                try:
                    with authenticated(root, PROJECT / 'state/icampus-credentials.json') as auth:
                        work_started = True
                        for meta in files:
                            if time.monotonic() >= deadline:
                                raise BudgetExpired()
                            meta['_deadline'] = min(deadline, time.monotonic() + 120)
                            try:
                                download(meta, auth, folder)
                                summary['downloaded'] += 1
                            except Exception as exc:
                                failure(summary, 'download', exc, meta)
                                continue
                            finally:
                                meta.pop('_deadline', None)
                            try:
                                _upload_material(meta, upload_folder, root, state,
                                                 seen, summary, deadline)
                            except Exception as exc:
                                failure(summary, 'upload', exc, meta)
                except BudgetExpired:
                    raise
                except Exception as exc:
                    failure(summary, 'icampus_auth', exc)
            if notices:
                # Each notice is an independent file. rclone checks for edits.
                for export in notices:
                    if time.monotonic() >= deadline:
                        raise BudgetExpired()
                    try:
                        _upload_notice(export, upload_folder, root, state, summary, deadline)
                    except Exception as exc:
                        failure(summary, 'notice_upload', exc, {'course_id': export['course_id'],
                                                              'file_id': 'notice-' + export['notice_id']})
    except BudgetExpired:
        failure(summary, 'budget', DriveError('전체 실행 시간 제한 초과'))
    finally:
        state['last_sync'] = now()
        try:
            write(root / 'drive.json', clean(state))
            write(root / 'drive-seen.json', clean(seen))
        except Exception as exc:
            failure(summary, 'state', exc)
        print(json.dumps(clean(summary), ensure_ascii=False, separators=(',', ':')))
    return 0 if work_started and not summary['failures'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='iCampus 강의 자료 Google Drive 동기화')
    parser.add_argument('--dry-run', action='store_true', help='오늘 저장된 수집 결과로 JSON 계획만 출력; 쓰기/다운로드/업로드 없음')
    parser.add_argument('--budget', type=int, default=1800, help='전체 실행 제한(초), 기본 1800')
    parser.add_argument('--existing', action='store_true', help='오늘 정상 수집 결과 재사용')
    args = parser.parse_args(argv)
    if args.budget <= 0:
        parser.error('--budget은 양수여야 합니다')
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s', stream=sys.stderr)
    root = PROJECT / 'state/icampus'
    deadline = time.monotonic() + args.budget
    try:
        if args.dry_run:
            # No new lock/directory/state files in plan mode. Lock an existing
            # inode if available to avoid reading halfway through another sync.
            with contextlib.ExitStack() as stack:
                if (root / 'drive.lock').exists():
                    lock = stack.enter_context(open(root / 'drive.lock', 'r', encoding='utf-8'))
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return sync(args, root, deadline)
        require_rclone()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(root / 'drive.lock', 'a', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with time_budget(args.budget):
                return sync(args, root, deadline)
    except BlockingIOError:
        print(json.dumps({'failures': [{'stage': 'lock', 'reason': 'Google Drive 동기화가 이미 실행 중'}]}, ensure_ascii=False))
        return 2
    except (Exception, BudgetExpired) as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) else ('전체 실행 시간 제한 초과' if isinstance(exc, BudgetExpired) else type(exc).__name__)
        print(json.dumps({'failures': [{'stage': 'startup', 'reason': reason}]}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
