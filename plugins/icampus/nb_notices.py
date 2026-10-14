"""One readable Markdown file per collected announcement."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from plugins.icampus.icampus import TZ, write
from plugins.icampus.nb_drive import DriveError, course_destination, course_label, course_labels
from plugins.icampus.nb_files import safe_filename


def notice_id(notice):
    value = str(notice.get('notice_id') or str(notice.get('id') or '').rsplit(':', 1)[-1])
    if not value.isdecimal():
        raise DriveError('Invalid iCampus notice ID')
    return value


def notice_filename(course_id, course_name, notice):
    course_destination(course_id)
    suffix = f' [{notice_id(notice)}].md'
    title = ' '.join(str(notice.get('title') or 'Untitled notice').split())
    stem = safe_filename(f'{course_label(course_name)} - {title}'.replace('/', ' - ').replace('\\', ' - '))
    while len((stem + suffix).encode('utf-8')) > 150:
        stem = stem[:-1]
    return stem + suffix


def _date(notice):
    # The collector currently uses posted_at; older inventories use created_at.
    return str(notice.get('created_at') or notice.get('posted_at') or '').strip()


def _order(notice):
    try:
        at = datetime.fromisoformat(_date(notice).replace('Z', '+00:00'))
        if at.tzinfo is None:
            at = at.replace(tzinfo=TZ)
        at = at.astimezone(timezone.utc)
    except ValueError:
        at = datetime.min.replace(tzinfo=timezone.utc)
    # A stable tie-breaker keeps the export identical if collection order changes.
    return at, str(notice.get('id') or ''), str(notice.get('title') or ''), str(notice.get('body') or '')


def render_notice(course_name, notice):
    """Preserve one notice's title, date, source and plain body text."""
    title = ' '.join(str(notice.get('title') or 'Untitled notice').split())
    body = str(notice.get('body') or '').strip() or '(No body text)'
    fields = [f'# {title}', f'Course: {course_label(course_name)}',
              f'Date: {_date(notice) or "Unknown"}']
    if notice.get('url'):
        fields.append(f'Source: {notice["url"]}')
    return '\n\n'.join([*fields, body]) + '\n'


def plan_notices(collected, labels=None):
    """Plan separate exports; courses without announcements have no row."""
    names = labels if labels is not None else course_labels(collected['courses'])
    grouped = {}
    for item in collected['items']:
        if isinstance(item, dict) and item.get('kind') == 'announcement':
            cid = str(item.get('course_id') or '')
            course_destination(cid)
            grouped.setdefault(cid, []).append(item)
    exports, seen = [], set()
    for cid, items in sorted(grouped.items()):
        for item in sorted(items, key=_order, reverse=True):
            nid = notice_id(item)
            if (cid, nid) in seen:
                continue
            seen.add((cid, nid))
            name = names.get(cid) or course_label(item.get('course_name') or cid)
            exports.append({'course_id': cid, 'course_name': name, 'notice_id': nid,
                            'filename': notice_filename(cid, name, item), 'notice_count': 1,
                            'content': render_notice(name, item),
                            'drive_destination': course_destination(cid, name)})
    return exports


def write_notices(export, course_folder):
    """Materialize a planned export inside the temporary upload folder."""
    path = Path(course_folder) / export['filename']
    write(path, export['content'])
    return path
