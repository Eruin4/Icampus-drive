"""Individual notice files, readable subjects, and real uploader orchestration."""
import copy
import contextlib
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from plugins.icampus import icampus, nb_drive, nb_judge, nb_notices, nb_sync


def collection(items):
    return {'checked_at': icampus.now(), 'status': 'collected',
            'courses': [{'id': 7, 'name': '운영체제_SWE3001_41(교수)'},
                        {'id': 8, 'name': 'Empty'}, {'id': 9, 'name': '자료구조'}],
            'items': items}


def test_each_notice_has_its_own_file_and_body(notice, tmp_path):
    items = [notice, {**notice, 'notice_id': 12, 'title': '과제 안내', 'body': 'Separate homework body'}]
    data = collection(items)
    before = copy.deepcopy(data)
    exports = nb_notices.plan_notices(data)
    assert len(exports) == 2
    assert len({row['filename'] for row in exports}) == 2
    for row in exports:
        assert row['filename'].startswith('운영체제 - ')
        assert row['notice_count'] == 1
        assert row['drive_destination'] == 'muse:SERVER-MIRROR/icampus/운영체제/'
        path = nb_notices.write_notices(row, tmp_path)
        assert path.read_text() == row['content']
        assert row['content'].count('\n\nCourse: ') == 1
    exam = next(row for row in exports if row['notice_id'] == '11')
    assert notice['body'] in exam['content']
    assert 'Separate homework body' not in exam['content']
    assert data == before
    assert nb_notices.plan_notices(collection([])) == []


def test_dates_timezones_and_empty_bodies(notice):
    items = [{**notice, 'notice_id': 1, 'title': 'Old', 'created_at': '2026-10-01T09:00:00+09:00', 'body': None},
             {**notice, 'notice_id': 2, 'title': 'Newest', 'created_at': '2026-10-01T01:00:00Z', 'body': 'Line one\nLine two'},
             {**notice, 'notice_id': 3, 'title': 'Unknown', 'created_at': None, 'body': '   '}]
    exports = nb_notices.plan_notices(collection(items))
    assert [row['notice_id'] for row in exports] == ['2', '1', '3']
    assert 'Line one\nLine two' in exports[0]['content']
    assert 'Date: Unknown' in exports[2]['content']
    assert '(No body text)' in exports[1]['content']
    assert '(No body text)' in exports[2]['content']
    assert 'Source: ' + notice['url'] in exports[0]['content']


def test_collector_posted_at_and_deterministic_ties():
    a = icampus.item_record(7, 'announcement', {'id': 11, 'title': 'A',
        'message': '<p>Hello &amp; welcome</p>', 'created_at': '2026-10-01T00:00:00Z'})
    b = icampus.item_record(7, 'announcement', {'id': 12, 'title': 'B',
        'message': '<p>Read this</p>', 'posted_at': '2026-10-02T00:00:00Z'})
    c = {**b, 'id': '7:announcement:13', 'title': 'C'}
    first = nb_notices.plan_notices(collection([a, b, c]))
    assert first == nb_notices.plan_notices(collection([c, a, b]))
    assert [row['notice_id'] for row in first] == ['13', '12', '11']
    assert 'Hello & welcome' in first[2]['content']


def test_identical_titles_and_long_names_stay_unique(notice):
    items = [{**notice, 'notice_id': nid, 'title': '공지/제목 ' * 100} for nid in [11, 12]]
    exports = nb_notices.plan_notices(collection(items))
    assert len({row['filename'] for row in exports}) == 2
    for row in exports:
        assert len(row['filename'].encode()) <= 150
        assert row['filename'].endswith(f' [{row["notice_id"]}].md')
        assert '/' not in row['filename']


@pytest.mark.parametrize('course_id', ['../7', '7/8', '', 'course'])
def test_filename_rejects_unsafe_course_ids(course_id, notice):
    with pytest.raises(nb_drive.DriveError):
        nb_notices.notice_filename(course_id, 'Subject', notice)


@pytest.fixture
def drive_run(isolated_home, monkeypatch):
    monkeypatch.setattr(nb_sync, 'PROJECT', isolated_home)
    monkeypatch.setattr(nb_drive, 'require_rclone', lambda: '/mock/rclone')
    monkeypatch.setattr(nb_sync, 'require_rclone', lambda: '/mock/rclone')
    root = isolated_home / 'state/icampus'
    root.mkdir(parents=True)
    calls = []
    def run(command, **kwargs):
        content = Path(command[2]).read_text() if command[1] == 'copy' else None
        calls.append((command, content, kwargs))
        return Mock(returncode=0)
    monkeypatch.setattr(nb_drive.subprocess, 'run', run)
    return root, calls


def save_collection(root, items):
    icampus.write(root / 'last-collected.json', collection(items))


def test_sync_real_uploader_rechecks_individual_notice_edits(drive_run, notice, monkeypatch, capsys):
    root, calls = drive_run
    judgment = Mock(side_effect=AssertionError('unexpected judgment'))
    monkeypatch.setattr(nb_judge, 'judge', judgment)
    for body in [notice['body'], notice['body'], 'Updated notice body']:
        save_collection(root, [{**notice, 'body': body}])
        assert nb_sync.main(['--existing']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['notice_files'] == result['notices_uploaded'] == result['uploaded'] == 1
        assert result['new_files'] == 0
        assert body in calls[-1][1]
        command = calls[-1][0]
        assert command[:2] == ['/mock/rclone', 'copy']
        assert Path(command[2]).name == '운영체제 - 시험 안내 [11].md'
        assert command[3] == 'muse:SERVER-MIRROR/icampus/운영체제/'
        assert calls[-1][2]['timeout'] > 0
    assert len(calls) == 3
    judgment.assert_not_called()
    state = json.loads((root / 'drive.json').read_text())
    assert list(state['notice_uploads']) == ['7:11']
    assert state['courses']['7']['drive_folder_name'] == '운영체제'
    assert state['uploaded'] == {}
    assert json.loads((root / 'drive-seen.json').read_text()) == {}


def test_title_edit_renames_existing_notice_instead_of_leaving_duplicates(drive_run, notice, capsys):
    root, calls = drive_run
    save_collection(root, [notice])
    assert nb_sync.main(['--existing']) == 0
    capsys.readouterr()
    save_collection(root, [{**notice, 'title': '변경된 시험 안내'}])
    assert nb_sync.main(['--existing']) == 0
    capsys.readouterr()
    assert [call[0][1] for call in calls] == ['copy', 'moveto', 'copy']
    assert calls[1][0][2].endswith('/운영체제 - 시험 안내 [11].md')
    assert calls[1][0][3].endswith('/운영체제 - 변경된 시험 안내 [11].md')
    state = json.loads((root / 'drive.json').read_text())
    assert len(state['notice_uploads']) == 1
    assert state['notice_uploads']['7:11']['drive_path'] == calls[1][0][3]


def test_renamed_notice_copy_failure_retries_without_repeating_move(drive_run, notice, monkeypatch, capsys):
    root, calls = drive_run
    save_collection(root, [notice])
    assert nb_sync.main(['--existing']) == 0
    capsys.readouterr()
    original_run = nb_drive.subprocess.run
    def fail_copy(command, **kwargs):
        if command[1] == 'copy':
            return Mock(returncode=1)
        return original_run(command, **kwargs)
    save_collection(root, [{**notice, 'title': 'New title'}])
    monkeypatch.setattr(nb_drive.subprocess, 'run', fail_copy)
    assert nb_sync.main(['--existing']) == 1
    capsys.readouterr()
    monkeypatch.setattr(nb_drive.subprocess, 'run', original_run)
    assert nb_sync.main(['--existing']) == 0
    capsys.readouterr()
    assert [call[0][1] for call in calls] == ['copy', 'moveto', 'copy']


def test_dry_run_plans_separate_notices_without_writes(drive_run, notice, monkeypatch, capsys):
    root, calls = drive_run
    save_collection(root, [notice, {**notice, 'notice_id': 12}, {**notice, 'course_id': 9}])
    before = {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    forbidden = Mock(side_effect=AssertionError('dry-run must be read-only'))
    for name in ('write', 'write_notices', 'upload_file', 'move_file', 'require_rclone', 'authenticated'):
        monkeypatch.setattr(nb_sync, name, forbidden)
    assert nb_sync.main(['--dry-run']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['candidates'] == []
    assert len(plan['notice_files']) == 3
    assert {row['drive_destination'] for row in plan['notice_files']} == {
        'muse:SERVER-MIRROR/icampus/운영체제/', 'muse:SERVER-MIRROR/icampus/자료구조/'}
    assert {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
    assert not (root / 'drive.lock').exists()
    assert calls == []
    forbidden.assert_not_called()


def test_notice_failure_does_not_block_another_notice_in_same_course(drive_run, notice, monkeypatch, capsys):
    root, calls = drive_run
    save_collection(root, [notice, {**notice, 'notice_id': 12}, {**notice, 'course_id': 9}])
    real_upload = nb_sync.upload_file
    def fail_one(cid, path, *args, **kwargs):
        if cid == '7' and '[11]' in path.name:
            raise nb_drive.DriveError('test failure')
        return real_upload(cid, path, *args, **kwargs)
    monkeypatch.setattr(nb_sync, 'upload_file', fail_one)
    assert nb_sync.main(['--existing']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['notices_uploaded'] == 2
    assert result['failures'] == [{'stage': 'notice_upload', 'reason': 'test failure', 'key': '7:notice-11'}]
    monkeypatch.setattr(nb_sync, 'upload_file', real_upload)
    assert nb_sync.main(['--existing']) == 0
    assert json.loads(capsys.readouterr().out)['notices_uploaded'] == 3


def test_zero_notices_skips_export(drive_run, capsys):
    root, calls = drive_run
    save_collection(root, [])
    assert nb_sync.main(['--existing']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['notice_files'] == result['notices_uploaded'] == 0
    assert calls == []


@pytest.mark.parametrize('file_outcome', ['ordinary', 'previously_rejected', 'download_failure'])
def test_notices_upload_alongside_materials(drive_run, notice, monkeypatch, capsys, file_outcome):
    root, calls = drive_run
    save_collection(root, [notice, {**notice, 'course_id': 9},
        {'kind': 'file', 'course_id': 7, 'file_id': 12, 'filename': 'lecture.txt'}])
    if file_outcome == 'previously_rejected':
        icampus.write(root / 'drive-seen.json', {'7:12': {'reason': 'KingoGPT 제외'}})
    monkeypatch.setattr(nb_sync, 'authenticated', lambda *args: contextlib.nullcontext(object()))
    def download(meta, auth, folder):
        if file_outcome == 'download_failure':
            raise RuntimeError('test download failure')
        path = Path(folder) / 'lecture.txt'
        path.write_text('Lecture material')
        meta.update(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(nb_sync, 'download', download)
    assert nb_sync.main(['--existing']) == int(file_outcome == 'download_failure')
    result = json.loads(capsys.readouterr().out)
    assert result['notices_uploaded'] == 2
    assert len(calls) == (2 if file_outcome == 'download_failure' else 3)
    assert Path(calls[-1][0][2]).name == '자료구조 - 시험 안내 [11].md'
    assert {call[0][3] for call in calls} == {
        'muse:SERVER-MIRROR/icampus/운영체제/', 'muse:SERVER-MIRROR/icampus/자료구조/'}
