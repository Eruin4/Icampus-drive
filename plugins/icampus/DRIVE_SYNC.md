# iCampus file sync to Google Drive

Run the existing pipeline from the Hermes repository root:

```bash
python -m plugins.icampus.nb_sync --dry-run --existing
python -m plugins.icampus.nb_sync --existing --budget 1800
```

The flags remain `--dry-run`, `--existing`, and `--budget`. Without
`--existing`, sync first runs the iCampus collector as before. Dry-run only
prints a plan; it does not download, upload, or require rclone.

Install rclone on PATH and configure the existing `muse` remote with
`rclone config`, with a Google authorization that allows uploads. Files go to
`muse:SERVER-MIRROR/icampus/<subject name>/`. Folder names omit the collector's
course-code, section and teacher suffix. IDs remain internal checkpoint keys;
duplicate subject sections get a distinguishing suffix. Upload filenames keep the
existing deterministic `<course_id>-<file_id>-<original filename>` format.
The uploader preserves any subdirectories beneath the staged course folder;
the downloader's private hash directories are not part of the upload path.

Each file uses `rclone copy`. Identical remote files are skipped by rclone on
retries and changed contents can be updated.
Each downloaded course file is copied immediately. There is no filename,
extension, size, word-count, or KingoGPT selection step. Authentication and
download error handling remain in place. SHA256 is recorded for checkpoints;
identical content in different course files is still uploaded under each name.

Each collected announcement is a separate Markdown file named
`<subject name> - <notice title> [<notice ID>].md` in its subject folder.
Each file contains the subject, title, date, source link and one notice body.
IDs keep identical titles distinct. Each run copies the current notice content;
if its title changes, the existing file is renamed before the copy so it stays
one file per notice. Courses without announcements have no notice export.
The old combined `<course_id>-notices.md` exports were retired during migration.

Drive checkpoints use `state/icampus/drive.json`, `drive-seen.json`, and
`drive.lock` beneath the configured Hermes home. On the first Drive sync,
course records and semester metadata are read from the existing
`notebooklm.json`. Every course in the current-semester collection is included;
legacy `course_scope` allowlists are removed from Drive checkpoints and ignored.
NotebookLM upload/seen history is not used to suppress
Drive uploads, so existing collected files are uploaded to Drive. Later
runs use the Drive checkpoints. `courses.*.drive_folder_name` preserves folder
labels, and `notice_uploads` tracks individual notice paths for title edits.
Only successful material uploads suppress files;
previous policy rejections and failed downloads/uploads remain eligible for retry,
and sync returns a nonzero status when it records failures.

`nb_notebooklm.py`, `nb_login.py`, and the old NotebookLM checkpoint files
are not part of this repository; they remain in the author's Hermes tree for
reverting the destination change.

Uploads need an rclone remote named `muse` authorized with the full `drive`
scope (a read-only remote cannot upload). If your upload configuration lives
in a separate file, point rclone at it explicitly, e.g.
`RCLONE_CONFIG=/path/to/icampus-upload.conf`.
