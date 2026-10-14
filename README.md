# Icampus-drive

Sync course materials and announcements from SKKU iCampus
(Canvas, `canvas.skku.edu`) to Google Drive, one folder per subject, so the
files can be picked up and processed downstream.

```bash
python -m plugins.icampus.nb_sync --dry-run --existing   # print the plan only
python -m plugins.icampus.nb_sync --existing --budget 1800
```

Files land in `muse:SERVER-MIRROR/icampus/<subject name>/` via `rclone copy`.
Every collected file is uploaded as-is — there is no filename, extension,
size, word-count, or LLM selection step — and every announcement becomes its
own Markdown file named `<subject name> - <notice title> [<notice ID>].md`.
See [plugins/icampus/DRIVE_SYNC.md](plugins/icampus/DRIVE_SYNC.md) for the
full behavior contract (checkpoints, retries, title-edit renames).

## A collaboration between two projects

This code is where two of the author's projects meet:

- The read-only collector (`icampus.py`) started life inside
  [kingoGPT](https://github.com/Eruin4/kingoGPT), the author's local LLM
  stack.
- It then moved into the author's Hermes home-automation/agent tree as the
  `plugins/icampus` plugin, where the Drive destination (`nb_sync`,
  `nb_drive`, `nb_files`, `nb_notices`) was built on top of it.

`nb_filter.py` and `nb_judge.py` are leftovers from the earlier
NotebookLM-destination era. The Drive sync never calls them — the tests
patch them to raise, so any regression that reintroduces filtering or
KingoGPT judgment fails loudly — they are kept for that regression contract
and for reference.

## Layout

| Path | What |
| --- | --- |
| `plugins/icampus/icampus.py` | Read-only iCampus collector (Canvas API + browser login) |
| `plugins/icampus/nb_sync.py` | Bounded, failure-isolated sync driver |
| `plugins/icampus/nb_drive.py` | rclone upload/move helpers and Drive folder naming |
| `plugins/icampus/nb_files.py` | Download and staging of course files |
| `plugins/icampus/nb_notices.py` | Per-notice Markdown export |
| `plugins/icampus/nb_filter.py`, `nb_judge.py` | Legacy NotebookLM-era selection/judgment (unused by Drive sync) |
| `plugins/icampus/DRIVE_SYNC.md` | Behavior contract for the Drive sync |
| `tests/plugins/icampus/` | Collector / Drive / sync unit tests |
| `plugins/icampus/tests/` | Announcement export tests |
| `hermes_constants.py` | Minimal stand-in so the plugin imports standalone (see its docstring) |

Not included: the calendar-sync modules, the NotebookLM uploader, the
manual login helper, and all runtime state. Credentials
(`state/icampus-credentials.json`, rclone configs, OAuth tokens) live under
`HERMES_HOME` on the host and are never part of this repository.

## Setup

- Python 3.11+
- `pip install -r plugins/icampus/requirements.txt`
- [rclone](https://rclone.org/) on `PATH` with a remote named `muse`
  authorized with the full `drive` scope (read-only cannot upload)
- iCampus credentials at `$HERMES_HOME/state/icampus-credentials.json`

## Tests

```bash
pip install pytest requests python-dotenv
python -m pytest tests/ plugins/icampus/tests/
```

The tests are fully offline: network, subprocesses, and credentials are
mocked or blocked by fixtures.
