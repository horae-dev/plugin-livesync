# Changelog

## 0.1.3

- The probe reads the remote's milestone document (read-only). A remote locked by a rebuild from
  Obsidian is reported as the problem `remote_locked` (also `remote.locked` in `status.json`),
  with the fix in `docs/setup.md`, instead of only `initial_replication_failed`. The CLI's node
  id is unknown to the plugin, so the problem is reported whenever the remote is locked.
