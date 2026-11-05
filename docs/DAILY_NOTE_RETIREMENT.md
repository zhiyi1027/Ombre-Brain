# Daily-note upload retirement candidate

This local candidate retires the self-authored handoff-note upload path. It is
not deployed and does not change existing servers, scheduler units, or client
configuration.

- The authenticated internal and Dashboard upload endpoints return HTTP 410
  without reading the request body or calling a writer. Service-level upload
  methods fail without writing files.
- `scripts/sync-daily-note.py` is a no-IO retirement notice with exit code 2.
  Existing scheduled callers must be retired during a separately authorized
  deployment; running this candidate does not change those callers.
- The Dashboard paste/upload UI is removed. Old note archives remain readable
  through the existing authenticated read surface; their bytes are preserved.
- Old notes no longer feed new impressions or create pending-generation days.
  Raw transcript and ordinary memory/feel/plan evidence remain supported.
- The prompt is versioned v8. An unchanged v7 impression remains valid; changed
  evidence follows the usual regeneration path. Existing manual impression
  overrides and their restoration behavior are retained.
- `OMBRE_DAILY_NOTE_TOKEN` remains a legacy fallback for private-continuity auth.
  This change does not rotate or break that unrelated credential path.

The existing raw-input budget/trimming path is still present. Large-volume
raw-only map/reduce generation, full source coverage, durable per-chunk caches,
provider selection, thinking-mode configuration, and billing limits are separate
work. Do not treat this retirement patch as an 800k-token processing acceptance.

Validation: 148 synthetic tests passed across daily continuity, nightly dreams,
private continuity, and HTTP application assembly. Python audit guard denied
all network access and confined test writes to a disposable task directory.
Dashboard inline JavaScript passed Node syntax checking; no browser visual
acceptance is claimed. No real notes, transcripts, credentials, or model API
calls were used.
