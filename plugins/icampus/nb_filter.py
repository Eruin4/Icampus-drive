"""Deterministic file selection and conservative NotebookLM limit checks."""
from __future__ import annotations
import codecs
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from plugins.icampus.nb_files import MAX_BYTES

EXTENSIONS = {'pdf', 'ppt', 'pptx', 'doc', 'docx', 'txt', 'md'}
ADMIN = ('출석부', '성적', '설문', 'attendance', 'gradebook', 'roster', 'survey')
MAX_WORDS = 500_000


def extension(meta):
    return Path(meta.get('filename') or meta.get('title', '')).suffix.lower().lstrip('.')


def word_units(text):
    # Count CJK characters separately: whitespace alone undercounts Korean/Chinese.
    pattern = r'[\u3400-\u9fff\uac00-\ud7af\u3040-\u30ff]|[^\s\u3400-\u9fff\uac00-\ud7af\u3040-\u30ff]+'
    count = 0
    for _ in re.finditer(pattern, text):
        count += 1
        if count > MAX_WORDS:
            break
    return count


def word_count(path):
    """Check text/OOXML locally. Binary formats also face Google's server cap.

    PDF and legacy Office have no reliable stdlib text extractor. They must reach
    processed status in NotebookLM; a server word-limit rejection is a failure,
    never a successful upload. No file contents are sent to the judgment model.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ('.txt', '.md'):
        with path.open('rb') as stream:
            prefix = stream.read(4)
        encodings = ['utf-16'] if prefix.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'cp949']
        for encoding in encodings:
            count = 0
            decoder = codecs.getincrementaldecoder(encoding)()
            try:
                with path.open('rb') as stream:
                    while chunk := stream.read(64 * 1024):
                        # Counting a word split at a chunk boundary twice is
                        # conservative and keeps memory bounded for large text.
                        count += word_units(decoder.decode(chunk))
                        if count > MAX_WORDS:
                            return count
                    count += word_units(decoder.decode(b'', final=True))
                return count
            except UnicodeDecodeError:
                if encoding == encodings[-1]:
                    raise
    if ext in ('.docx', '.pptx'):
        count = 0
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist() if info.filename.endswith('.xml')) > MAX_BYTES:
                raise ValueError('압축 해제 XML 총 크기 한도 초과')
            for info in archive.infolist():
                # Include notes, comments and other text parts, conservatively.
                if not info.filename.endswith('.xml'):
                    continue
                if info.file_size > MAX_BYTES:
                    raise ValueError('압축 해제 XML 크기 한도 초과')
                with archive.open(info) as stream:
                    for _, node in ElementTree.iterparse(stream, events=('end',)):
                        if node.tag.rsplit('}', 1)[-1] == 't' and node.text:
                            count += word_units(node.text)
                        node.clear()
                        if count > MAX_WORDS:
                            return count
        return count
    return None


def filter_candidates(files, state=None):
    """Optional state supports standalone use; the sync passes uploaded digests.

    A caller using the one-argument API can mark rows with already_uploaded=True.
    Download failures retain size_hint for planning but are never uploadable.
    """
    accepted, rejected = [], []
    digests = set((state or {}).get('uploaded', {}))
    for row in files:
        reason = None
        ext = extension(row)
        name = (str(row.get('title', '')) + ' ' + str(row.get('filename', ''))).casefold()
        path = Path(row['path']) if row.get('path') else None
        try:
            size = path.stat().st_size if path and path.is_file() else row.get('size', row.get('size_hint'))
            size = int(size) if size is not None else None
            if size is not None and size < 0:
                raise ValueError('negative size')
        except (OSError, TypeError, ValueError):
            rejected.append({**row, 'reason': '파일 크기 확인 실패'})
            continue
        if ext not in EXTENSIONS:
            reason = '지원하지 않는 확장자'
        elif any(pattern in name for pattern in ADMIN):
            reason = '행정 자료 파일명'
        elif size is not None and size > MAX_BYTES:
            reason = '파일 크기 200MB 초과'
        elif row.get('already_uploaded') or (row.get('sha256') and row['sha256'] in digests):
            reason = '동일 SHA256 자료 이미 처리됨'
        elif row.get('download_error'):
            reason = '다운로드 실패'
        if reason is None and path and path.is_file():
            try:
                count = word_count(path)
                row['word_count'] = count
                if count is not None and count > MAX_WORDS:
                    reason = '출처 단어 수 500,000 초과 (보수적 계산)'
            except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, UnicodeError):
                reason = '파일 형식/단어 수 검사 실패'
        if reason:
            rejected.append({**row, 'reason': reason})
        else:
            accepted.append(row)
            if row.get('sha256'):
                digests.add(row['sha256'])
    return accepted, rejected
