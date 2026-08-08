# ADR: Dream uses a rolling 48-hour creation window

## Decision

`dream()` returns ordinary candidate buckets created during the rolling 48 hours
before the call. Candidate admission uses `created_at`, with `created` retained as
the legacy metadata fallback. `last_active` does not admit a bucket.

The public `window_hours` argument remains temporarily accepted for client
compatibility, but it no longer changes the window. The existing exclusions,
complete-body rendering, 40-candidate safety cap, core context, active plans, and
feel-history sections remain unchanged.

## Why

The previous-calendar-day rule produced too little material, especially early in
the day, so meaningful events often received no second chance to become a `feel`.
A rolling 48-hour overlap gives recent experiences another opportunity for
sedimentation without turning later reads or edits of old buckets into new events.

## Trade-offs

- Consecutive dreams may show the same recent bucket more than once. This overlap
  is intentional; `dream` is optional and repetition gives unfinished material a
  second chance.
- A fixed window is less configurable, but keeps the meaning of `dream` stable
  across clients and prevents accidental narrow calls from starving reflection.
- Buckets with missing or invalid creation metadata are excluded rather than
  admitted by activity time.

## Rejected alternatives

- Previous local calendar day: too sparse and sensitive to time of day.
- `created OR last_active` within 48 hours: old buckets re-enter after reads,
  merges, or metadata edits, confusing activity with a new experience.
- Unbounded recent history: risks context overflow and weakens the meaning of
  “recent”; the existing 40-candidate cap remains.

## Tests required

- Include buckets created just inside the rolling 48-hour boundary.
- Exclude buckets created just outside it even when `last_active` is current.
- Honor `created_at` and legacy `created`, and exclude future/invalid timestamps.
- Prove public `window_hours` values do not change the fixed 48-hour window.
