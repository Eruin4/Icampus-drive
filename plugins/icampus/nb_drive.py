"""Upload selected iCampus files to the Google Drive bridge with rclone."""
from __future__ import annotations
import shutil
import subprocess
import time
import re
import unicodedata
from collections import Counter
from pathlib import Path

from plugins.icampus.nb_files import safe_filename

DESTINATION = 'muse:SERVER-MIRROR/icampus/'
RCLONE_HELP = 'rclone was not found on PATH. Install rclone and configure a remote named muse with rclone config.'


class DriveError(RuntimeError):
    """An operator-readable upload error without third-party output."""


def require_rclone():
    executable = shutil.which('rclone')
    if executable is None:
        raise DriveError(RCLONE_HELP)
    return executable


def course_label(name):
    """Readable subject name, without the collector's code/section/teacher tail."""
    name = unicodedata.normalize('NFC', str(name))
    name = re.sub(r'_[A-Za-z]+\d+_[A-Za-z0-9]+(?:\([^)]*\))?$', '', name)
    name = re.sub(r'[/\\]', ' - ', name)
    return safe_filename(' '.join(name.split()))


def course_labels(courses, saved_courses=None):
    saved = saved_courses or {}
    names = {str(row['id']): course_label(row.get('name') or row['id'])
             for row in courses if isinstance(row, dict) and 'id' in row}
    labels = {cid: saved.get(cid, {}).get('drive_folder_name') or name
              for cid, name in names.items()}
    counts = Counter(labels.values())
    for cid, label in list(labels.items()):
        if counts[label] > 1:
            # Different sections of one subject must not share an upload folder.
            if not saved.get(cid, {}).get('drive_folder_name'):
                labels[cid] = course_label(f'{label} ({cid})')
    return labels


def course_destination(course_id, course_name=None):
    """Use course IDs internally and readable subject names in Drive."""
    cid = str(course_id)
    if not cid.isdecimal():
        raise DriveError('Invalid iCampus course ID')
    return DESTINATION + course_label(course_name if course_name else cid) + '/'


def upload_file(course_id, file_path, course_folder, *, deadline=None, course_name=None):
    """Copy one survivor, preserving its name and path beneath course_folder.

    rclone copy compares existing files and skips identical content on retries.
    It never deletes remote files. No local success cache bypasses that check.
    """
    executable = require_rclone()
    path = Path(file_path).resolve()
    try:
        relative = path.relative_to(Path(course_folder).resolve())
    except ValueError:
        raise DriveError('Upload file must be inside its course folder') from None
    if not path.is_file():
        raise DriveError('Upload file does not exist')
    destination = course_destination(course_id, course_name)
    if relative.parent != Path('.'):
        destination += relative.parent.as_posix() + '/'
    timeout = None if deadline is None else deadline - time.monotonic()
    if timeout is not None and timeout <= 0:
        raise DriveError('Google Drive upload time budget exceeded')
    try:
        result = subprocess.run([executable, 'copy', str(path), destination],
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DriveError(RCLONE_HELP) from None
    except subprocess.TimeoutExpired:
        raise DriveError('Google Drive upload time budget exceeded') from None
    if result.returncode:
        # rclone output can contain private paths or authenticated URLs.
        raise DriveError(f'rclone copy failed (exit {result.returncode}). Check the configured muse remote.')
    return destination + relative.name


def move_file(source, destination, *, deadline=None):
    """Keep an edited notice's single file when its readable title changes."""
    if not source.startswith(DESTINATION) or not destination.startswith(DESTINATION):
        raise DriveError('Notice move must stay inside the iCampus Drive folder')
    timeout = None if deadline is None else deadline - time.monotonic()
    if timeout is not None and timeout <= 0:
        raise DriveError('Google Drive upload time budget exceeded')
    try:
        result = subprocess.run([require_rclone(), 'moveto', source, destination],
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DriveError(RCLONE_HELP) from None
    except subprocess.TimeoutExpired:
        raise DriveError('Google Drive upload time budget exceeded') from None
    if result.returncode:
        raise DriveError(f'rclone notice move failed (exit {result.returncode}). Check the configured muse remote.')
