"""Offline Drive upload contracts; every rclone invocation is mocked."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins.icampus import nb_drive


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / '101-learningx-5-강의 자료 01.txt'
        self.path.write_text('lecture material', encoding='utf-8')
        self.which = self.enterContext(patch.object(nb_drive.shutil, 'which', return_value='/usr/bin/rclone'))
        self.run = self.enterContext(patch.object(nb_drive.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')))

    def test_course_destination_and_filename_preservation(self):
        for course in ('101', 202):
            with self.subTest(course=course):
                destination = f'muse:SERVER-MIRROR/icampus/{course}/'
                result = nb_drive.upload_file(course, self.path, self.folder)
                self.assertEqual(result, destination + self.path.name)
                self.run.assert_called_with(
                    ['/usr/bin/rclone', 'copy', str(self.path), destination],
                    capture_output=True, text=True, timeout=None)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'lecture material')

    def test_nested_relative_path_is_preserved(self):
        nested = self.folder / 'week 02' / 'slides' / self.path.name
        nested.parent.mkdir(parents=True)
        nested.write_bytes(self.path.read_bytes())
        result = nb_drive.upload_file('101', nested, self.folder)
        self.assertEqual(result, 'muse:SERVER-MIRROR/icampus/101/week 02/slides/' + self.path.name)
        self.assertEqual(self.run.call_args.args[0][-1], 'muse:SERVER-MIRROR/icampus/101/week 02/slides/')

    def test_named_subject_folder_omits_numeric_id_and_section(self):
        result = nb_drive.upload_file('101', self.path, self.folder,
                                     course_name='운영체제_SWE3001_41(교수)')
        self.assertEqual(result, 'muse:SERVER-MIRROR/icampus/운영체제/' + self.path.name)
        self.assertEqual(self.run.call_args.args[0][-1], 'muse:SERVER-MIRROR/icampus/운영체제/')

    def test_same_subject_sections_have_distinct_stable_folders(self):
        courses = [{'id': 101, 'name': '운영체제_SWE3001_41(A)'},
                   {'id': 202, 'name': '운영체제_SWE3001_42(B)'}]
        labels = nb_drive.course_labels(courses)
        self.assertEqual(len(set(labels.values())), 2)
        self.assertEqual(labels, nb_drive.course_labels(list(reversed(courses))))
        saved = {'101': {'drive_folder_name': '운영체제'}}
        labels = nb_drive.course_labels(courses, saved)
        self.assertEqual(labels['101'], '운영체제')
        self.assertNotEqual(labels['202'], '운영체제')

    def test_identical_retry_is_successful_without_renaming(self):
        # Model rclone's comparison at the subprocess boundary. The second
        # copy succeeds with zero transfers; the uploader must accept that.
        remote, transfers, commands = {}, [], []

        def rclone_copy(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[1], 'copy')
            source = Path(command[2])
            target = command[3] + source.name
            content = source.read_bytes()
            if remote.get(target) != content:
                remote[target] = content
                transfers.append(target)
            return subprocess.CompletedProcess(command, 0, '', '')

        self.run.side_effect = rclone_copy
        first = nb_drive.upload_file('101', self.path, self.folder)
        second = nb_drive.upload_file('101', self.path, self.folder)
        self.assertEqual(first, second)
        self.assertEqual(commands[0], commands[1])
        self.assertEqual(transfers, [first])
        # Changed contents still get copied; --ignore-existing would break this.
        self.path.write_text('updated material', encoding='utf-8')
        nb_drive.upload_file('101', self.path, self.folder)
        self.assertEqual(transfers, [first, first])
        self.assertEqual(remote[first], b'updated material')

    def test_missing_rclone_has_install_and_remote_guidance(self):
        self.which.return_value = None
        with self.assertRaisesRegex(nb_drive.DriveError, 'Install rclone.*remote named muse'):
            nb_drive.upload_file('101', self.path, self.folder)
        self.which.assert_called_with('rclone')
        self.run.assert_not_called()

    def test_rclone_disappearing_before_spawn_has_same_guidance(self):
        self.run.side_effect = FileNotFoundError()
        with self.assertRaisesRegex(nb_drive.DriveError, 'Install rclone.*remote named muse'):
            nb_drive.upload_file('101', self.path, self.folder)

    def test_copy_failure_is_not_success_and_does_not_expose_output(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, '', 'https://private.example/?token=secret')
        with self.assertRaisesRegex(nb_drive.DriveError, 'exit 1.*muse remote') as error:
            nb_drive.upload_file('101', self.path, self.folder)
        self.assertNotIn('secret', str(error.exception))

    def test_deadline_bounds_copy_and_reports_timeout(self):
        with patch.object(nb_drive.time, 'monotonic', return_value=100):
            nb_drive.upload_file('101', self.path, self.folder, deadline=120)
        self.assertEqual(self.run.call_args.kwargs['timeout'], 20)
        self.run.side_effect = subprocess.TimeoutExpired('rclone', 20)
        with self.assertRaisesRegex(nb_drive.DriveError, 'time budget exceeded'):
            nb_drive.upload_file('101', self.path, self.folder)

    def test_expired_deadline_does_not_start_copy(self):
        with patch.object(nb_drive.time, 'monotonic', return_value=100):
            with self.assertRaisesRegex(nb_drive.DriveError, 'time budget exceeded'):
                nb_drive.upload_file('101', self.path, self.folder, deadline=99)
        self.run.assert_not_called()

    def test_invalid_course_and_outside_file_are_rejected(self):
        for course in ('', '../101', '101/other', 'muse:other'):
            with self.subTest(course=course), self.assertRaises(nb_drive.DriveError):
                nb_drive.upload_file(course, self.path, self.folder)
        with self.assertRaisesRegex(nb_drive.DriveError, 'inside its course folder'):
            nb_drive.upload_file('101', self.path, self.folder / 'another')
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
