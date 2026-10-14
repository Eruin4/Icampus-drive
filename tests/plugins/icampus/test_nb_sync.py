"""Direct Drive uploads using real inventory/state and stubbed I/O."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from plugins.icampus import nb_drive, nb_files, nb_filter, nb_judge, nb_sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.root = self.project / 'state/icampus'
        self.root.mkdir(parents=True)
        self.enterContext(patch.object(nb_sync, 'PROJECT', self.project))
        # main() sets the production umask; restore it after each test.
        mask = os.umask(0o077)
        os.umask(mask)
        self.addCleanup(os.umask, mask)
        self.events = []
        self.data = {
            'checked_at': nb_sync.now(), 'status': 'collected',
            'courses': [{'id': 101, 'name': '운영체제', 'term': '2026-2'},
                        {'id': 202, 'name': '자료구조', 'term': '2026-2'}],
            'items': [self.item(101, 1, '강의 01.txt'),
                      self.item(101, 2, 'survey.txt'),
                      self.item(101, 3, 'memo.txt'),
                      self.item(202, 4, '강의 02.txt')]}
        nb_sync.write(self.root / 'last-collected.json', self.data)
        self.enterContext(patch.object(nb_sync, 'authenticated', return_value=contextlib.nullcontext(object())))
        self.download = self.enterContext(patch.object(nb_sync, 'download', side_effect=self.fake_download))
        self.filter = self.enterContext(patch.object(nb_filter, 'filter_candidates', side_effect=AssertionError('unexpected filter')))
        self.judge = self.enterContext(patch.object(nb_judge, 'judge', side_effect=AssertionError('unexpected judgment')))
        self.addCleanup(self.filter.assert_not_called)
        self.addCleanup(self.judge.assert_not_called)
        self.subprocess = self.enterContext(patch.object(subprocess, 'run', side_effect=AssertionError('unmocked subprocess')))

    @staticmethod
    def item(cid, fid, filename):
        return {'kind': 'file', 'course_id': cid, 'file_id': fid,
                'filename': filename, 'title': filename, 'size_hint': 20}

    def fake_download(self, row, auth, folder):
        self.events.append('download')
        # Match the downloader's private hash directory and readable filename.
        path = Path(folder) / hashlib.sha256(nb_sync.file_key(row).encode()).hexdigest() / row['filename']
        path.parent.mkdir()
        content = ('lecture ' + nb_sync.file_key(row)).encode()
        path.write_bytes(content)
        row.update(path=str(path), size=len(content), sha256=hashlib.sha256(content).hexdigest())
        return path

    def fake_upload(self, cid, path, folder, *, deadline, course_name=None):
        self.events.append('drive')
        self.assertGreater(deadline, time.monotonic())
        self.assertEqual(Path(folder).name, cid)
        self.assertTrue(Path(path).is_file())
        self.assertNotIn(hashlib.sha256(Path(path).read_bytes()).hexdigest(), Path(path).name)
        return nb_drive.course_destination(cid, course_name) + Path(path).relative_to(folder).as_posix()

    def run_sync(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = nb_sync.sync(Namespace(existing=True, dry_run=False), self.root, time.monotonic() + 60)
        return code, json.loads(out.getvalue())

    def test_every_download_uploads_immediately_without_selection(self):
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ['download', 'drive'] * 4)
        self.assertEqual([call.args[0] for call in upload.call_args_list], ['101', '101', '101', '202'])
        self.assertEqual([call.args[1].name for call in upload.call_args_list],
                         ['101-1-강의 01.txt', '101-2-survey.txt', '101-3-memo.txt', '202-4-강의 02.txt'])
        self.assertEqual(summary['uploaded'], 4)
        self.assertEqual(summary['failures'], [])
        self.subprocess.assert_not_called()
        state, seen = nb_sync.read_state(self.root)
        self.assertEqual(len(state['uploaded']), 4)
        self.assertEqual(set(seen), {'101:1', '101:2', '101:3', '202:4'})

    def test_main_uses_real_drive_uploader_and_rerun_skips_processed_files(self):
        self.subprocess.side_effect = None
        self.subprocess.return_value = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(nb_drive.shutil, 'which', return_value='/usr/bin/rclone'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = nb_sync.main(['--existing', '--budget', '60'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['uploaded'], 4)
        self.assertEqual([call.args[0][1] for call in self.subprocess.call_args_list], ['copy'] * 4)
        self.assertEqual([call.args[0][-1] for call in self.subprocess.call_args_list],
                         ['muse:SERVER-MIRROR/icampus/운영체제/'] * 3 + ['muse:SERVER-MIRROR/icampus/자료구조/'])
        self.subprocess.reset_mock()
        self.download.reset_mock()
        code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['new_files'], 0)
        self.subprocess.assert_not_called()
        self.download.assert_not_called()

    def test_dry_run_is_read_only_and_does_not_require_rclone(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(nb_sync, 'require_rclone') as preflight, patch.object(nb_sync, 'upload_file') as upload:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = nb_sync.main(['--dry-run', '--existing', '--budget', '60'])
        plan = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(plan['dry_run'])
        self.assertEqual(self.events, [])
        self.assertEqual(len(plan['candidates']), 4)
        self.assertEqual(plan['candidates'][0]['drive_destination'], 'muse:SERVER-MIRROR/icampus/운영체제/')
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / 'drive.lock').exists())
        preflight.assert_not_called()
        self.download.assert_not_called()
        self.judge.assert_not_called()
        upload.assert_not_called()
        self.subprocess.assert_not_called()

    def test_legacy_allowlist_does_not_restrict_current_courses(self):
        legacy = {'courses': {'101': {'name': '운영체제', 'notebook_id': 'old-notebook'}},
                  'uploaded': {'a' * 64: {'course_id': '101', 'file_id': '1'}},
                  'course_scope': ['101'], 'semester': '2026-2', 'last_sync': 'old'}
        nb_sync.write(self.root / 'notebooklm.json', legacy)
        nb_sync.write(self.root / 'nb-seen.json', {'101:1': {'reason': '업로드 완료'}})
        original = (self.root / 'notebooklm.json').read_bytes()
        original_seen = (self.root / 'nb-seen.json').read_bytes()
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['out_of_scope'], 0)
        self.assertEqual(upload.call_count, 4)
        self.assertEqual(upload.call_args.args[0], '202')
        self.assertEqual((self.root / 'notebooklm.json').read_bytes(), original)
        self.assertEqual((self.root / 'nb-seen.json').read_bytes(), original_seen)
        state, _ = nb_sync.read_state(self.root)
        self.assertNotIn('course_scope', state)
        self.assertEqual(state['semester'], '2026-2')
        self.assertNotIn('a' * 64, state['uploaded'])

    def test_existing_drive_allowlist_does_not_restrict_current_courses(self):
        nb_sync.write(self.root / 'drive.json', {
            'courses': {}, 'uploaded': {}, 'course_scope': ['101'], 'semester': 'old semester'})
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['uploaded'], 4)
        self.assertEqual({call.args[0] for call in upload.call_args_list}, {'101', '202'})
        self.assertNotIn('course_scope', nb_sync.load(self.root / 'drive.json'))

    def test_failed_upload_stays_retryable_and_other_course_continues(self):
        def uploading(cid, *args, **kwargs):
            if cid == '101':
                raise nb_drive.DriveError('rclone copy failed (test)')
            return self.fake_upload(cid, *args, **kwargs)

        with patch.object(nb_sync, 'upload_file', side_effect=uploading):
            code, summary = self.run_sync()
        self.assertEqual(code, 1)
        self.assertEqual(summary['uploaded'], 1)
        self.assertEqual(summary['failures'][0]['stage'], 'upload')
        self.assertEqual(summary['failures'][0]['key'], '101:1')
        state, seen = nb_sync.read_state(self.root)
        self.assertNotIn('101:1', seen)
        self.assertEqual([row['course_id'] for row in state['uploaded'].values()], ['202'])
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['uploaded'], 3)
        self.assertEqual([call.args[0] for call in upload.call_args_list], ['101'] * 3)

    def test_prior_policy_rejections_are_uploaded_and_then_skipped(self):
        nb_sync.write(self.root / 'drive-seen.json', {
            '101:2': {'reason': '행정 자료 파일명'},
            '101:3': {'reason': 'KingoGPT 제외'}})
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload):
            code, summary = self.run_sync()
            self.assertEqual(code, 0)
            self.assertEqual(summary['uploaded'], 4)
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['new_files'], 0)

    def test_archive_and_large_size_hint_are_uploaded(self):
        self.data['items'] = [self.item(101, 5, 'survey.zip')]
        self.data['items'][0]['size_hint'] = 300_000_000
        nb_sync.write(self.root / 'last-collected.json', self.data)
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['uploaded'], 1)
        self.assertEqual(upload.call_args.args[1].name, '101-5-survey.zip')

    def test_download_has_no_legacy_notebooklm_size_limit(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.url = 'https://canvas.skku.edu/courses/101/files/5/download'
        response.headers = {'Content-Type': 'application/octet-stream', 'Content-Length': '300000000'}
        response.iter_content.return_value = [b'archive data']
        session = Mock()
        session.get.return_value = response
        row = {'course_id': '101', 'file_id': '5', 'filename': 'survey.zip'}
        with patch.object(nb_files, 'MAX_BYTES', 1), patch.object(nb_files, 'Client') as client:
            client.return_value.get.return_value = ({}, None)
            path = nb_files.download(row, session, self.project / 'downloads')
        self.assertEqual(path.read_bytes(), b'archive data')
        self.assertEqual(row['size'], len(b'archive data'))

    def test_learningx_file_viewer_clicks_filename_to_start_download(self):
        page = Mock()
        context, session, frame = Mock(), Mock(), Mock()
        page.frames = [frame]
        control = frame.locator.return_value
        control.count.return_value = 1
        control.first.is_visible.return_value = True
        handlers = {}
        page.on.side_effect = lambda event, callback: handlers.update({event: callback})
        asset = Mock(url='https://canvas.skku.edu/assets/custom.catalog.js', status=200,
                     headers={'content-type': 'application/octet-stream',
                              'content-disposition': 'attachment; filename="custom.catalog.js"'})
        asset.request.method = 'GET'
        page.goto.side_effect = lambda *args, **kwargs: handlers['response'](asset)
        event = Mock(url='https://lcms.skku.edu/file-download/abc')
        control.first.click.side_effect = lambda **kwargs: handlers['download'](event)
        context.cookies.return_value = []
        row = {'course_id': '101', 'module_item_id': '5'}
        url = nb_files._learningx_download_url(row, (context, page, session))
        self.assertEqual(url, event.url)
        control.first.click.assert_called_once()
        event.cancel.assert_not_called()
        self.assertIs(row['_browser_download'], event)
        page.remove_listener.assert_any_call('download', handlers['download'])

    def test_learningx_browser_download_keeps_authenticated_bytes(self):
        event = Mock(suggested_filename='honor pledge.docx')
        event.save_as.side_effect = lambda path: Path(path).write_bytes(b'authenticated document bytes')
        context, page, session = Mock(), Mock(), Mock()
        row = {'course_id': '101', 'file_id': 'learningx-5', 'module_item_id': '5',
               'source': 'learningx', 'filename': 'honor pledge.docx'}
        def resolve(meta, auth):
            meta['_browser_download'] = event
            return 'https://lcms.skku.edu/file-download/abc'
        with patch.object(nb_files, '_learningx_download_url', side_effect=resolve):
            path = nb_files.download(row, (context, page, session), self.project / 'downloads')
        self.assertEqual(path.read_bytes(), b'authenticated document bytes')
        self.assertEqual(path.name, 'honor pledge.docx')
        self.assertNotIn('_browser_download', row)
        session.get.assert_not_called()

    def test_download_failure_does_not_block_other_files_and_is_retryable(self):
        def downloading(row, auth, folder):
            if row['file_id'] == '2':
                raise RuntimeError('test download failure')
            return self.fake_download(row, auth, folder)

        with patch.object(nb_sync, 'download', side_effect=downloading), patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload):
            code, summary = self.run_sync()
        self.assertEqual(code, 1)
        self.assertEqual(summary['uploaded'], 3)
        self.assertEqual(summary['failures'], [{'stage': 'download', 'reason': 'RuntimeError', 'key': '101:2'}])
        with patch.object(nb_sync, 'upload_file', side_effect=self.fake_upload) as upload:
            code, summary = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(summary['uploaded'], 1)
        self.assertEqual(upload.call_args.args[1].name, '101-2-survey.txt')

    def test_missing_rclone_fails_main_before_download_or_state_writes(self):
        with patch.object(nb_drive.shutil, 'which', return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = nb_sync.main(['--existing'])
        self.assertEqual(code, 1)
        error = json.loads(out.getvalue())['failures'][0]
        self.assertEqual(error['stage'], 'startup')
        self.assertIn('Install rclone', error['reason'])
        self.assertIn('remote named muse', error['reason'])
        self.download.assert_not_called()
        self.subprocess.assert_not_called()
        self.assertFalse((self.root / 'drive.json').exists())
        self.assertFalse((self.root / 'drive.lock').exists())


if __name__ == '__main__':
    unittest.main()
