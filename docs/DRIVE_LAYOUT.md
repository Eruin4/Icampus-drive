# Server ↔ Google Drive layout

This repository does not live on its own: it writes into a Drive folder that
doubles as the file bridge between the author's home server and everything
that consumes the files afterwards. This page describes that layout.

## The bridge: `SERVER-MIRROR`

Google Drive is the fixed meeting point. The server has no public inbound
endpoint for the consumers, and the consumers (an external assistant
environment, "Spark") have no fixed compute of their own — so both sides
read and write the same Drive folder, `SERVER-MIRROR`, instead of talking to
each other directly.

```text
                        rclone copy (this repo, nb_sync)
  iCampus (Canvas) ──► home server ─────────────────────────► Google Drive
   collector             Hermes plugin                          SERVER-MIRROR/
                                                                      │
              ┌───────────────────────────────────────────────────────┤
              │ rclone mirror timer (15 min)                          │ read/process
              ▼                                                       ▼
  server workspace: server-mirror/                          Spark (downstream
  (local mirror of the same tree)                           processing)
```

## Drive side

```text
SERVER-MIRROR/
├── icampus/                        ← written by this repository
│   └── <subject name>/             one folder per subject; the readable
│       │                           subject name only — course codes,
│       │                           sections and teacher names are stripped
│       ├── <course_id>-learningx-<file_id>-<original filename>
│       │                           course materials, uploaded as collected
│       └── <subject name> - <notice title> [<notice ID>].md
│                                   one Markdown file per announcement
├── study-fall2026-midterms/        another producer using the same bridge:
│   └── NN-<topic>.md, index.md     a generated exam question bank
└── …                               anything else the server side publishes
                                    through the mirror lands at this level

SERVER-MIRROR-TRASH/                bisync safety net: Drive-side backups of
    └── <YYYY-MM-DD>/<run-id>/      files a sync run deletes or overwrites
```

Upload rules for `icampus/` are the behavior contract in
[../plugins/icampus/DRIVE_SYNC.md](../plugins/icampus/DRIVE_SYNC.md):
`rclone copy` only (never deletes on the Drive side), identical files are
skipped on retries, and a notice whose title changed is renamed with
`rclone moveto` so it stays one file per notice.

## Server side

- The same tree is mirrored into a `server-mirror/` directory in the server
  workspace, so server-side projects read Drive files as local files.
- The mirror is an rclone job on a systemd timer, every 15 minutes.
  It started as a one-way `rclone sync` (Drive → server) — which also meant
  local-only files placed in `server-mirror/` were deleted by the next run,
  never uploaded. It is being converted to two-way `rclone bisync`
  (Drive ↔ server) with a baseline resync, per-date trash folders on both
  sides (`server-mirror-trash/` locally, `SERVER-MIRROR-TRASH/` on Drive),
  and a lock so runs cannot overlap.
- Producers on the server that publish *through* the bridge therefore write
  to Drive directly (`rclone copy` to `muse:SERVER-MIRROR/…`, like this
  repo does) instead of dropping files into the local mirror and hoping.

## Two rclone remotes, on purpose

| Remote / config | Scope | Used for |
| --- | --- | --- |
| mirror config | `drive.readonly` originally | Drive → server mirroring |
| upload config | full `drive` | this repo's `rclone copy` / `moveto` into `SERVER-MIRROR/icampus/` |

Uploads use a separately authorized configuration with the full `drive`
scope; point `RCLONE_CONFIG` at it when it is not the default config.
Credentials and tokens live on the host under the service user's config
directory — they are never in this repository.
