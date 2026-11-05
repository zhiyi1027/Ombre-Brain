# Night clerk privacy input boundary — local prototype

The future night clerk must never receive excluded intimate passages. Local
classification happens before day selection, chunking, quotes, cache storage
or model requests. `clerk_privacy.py` and `clerk_input.py` implement the first
isolated input component. They are now wired behind disabled-by-default clerk gates. The archive, cache,
publication and Breath integration are documented in [NIGHT_CLERK.md](NIGHT_CLERK.md).
A real subscription adapter is not configured.

## Contract

- Supply a complete, assembled timeline and explicitly declare
  `history_complete=True`. A day slice cannot establish the opening state.
- Timestamp, ID, speaker or revision conflicts reject the input; unassembled
  fragments also reject it. Error messages do not echo source text.
- The scan is chronological and global across the supplied conversations.
  Backtracking can exclude earlier rows. Day selection happens afterwards.
- Long/dense signals and short/dense signals can confirm an interval. A weaker
  signal starts an uncertain interval that is also withheld.
- Both participants' complete messages remain excluded inside an interval,
  including short replies and aftercare. Aftercare terms alone in an ordinary
  conversation do not start one.
- Timeout/silence only permit considering a closing phase. They never release
  messages by themselves. Returning to ordinary input requires multiple
  spaced daily-context messages; these confirmation messages remain withheld.
- Timestamped trusted positive markers can start an interval. No local flag
  file is read, and absence of markers is never a safety signal.
- The recent tail remains withheld to allow backtracking when more context
  arrives. Unknown boundaries remain withheld rather than guessed safe.

Initial synthetic-test defaults are 10 minutes of backtracking, 10 minutes of
tail buffering, 30-minute timeout/silence candidates, and at least two ordinary
messages spanning two minutes. These are provisional engineering values, not
measured privacy accuracy or user-approved final thresholds.

The dictionary is intentionally conservative. Ordinary conversation may be
excluded, and unrecognized wording may still evade detection. The synthetic
scenario tests do not establish zero leakage on real conversations.

## Output and revisions

Coverage distinguishes `allowed`, `intentional_skip` and `uncertain_hold`.
Receipts include IDs, times, statuses, reasons and opaque revision hashes; they
do not include text, matched words or thinking. Receipts themselves are private
metadata and must not enter Git or ordinary public logs for real records.

`prepare_clerk_day` emits original allowed text with source IDs and times.
Native thinking remains available for archive viewing, but is ignored by the clerk: it cannot affect classification, source
revisions, caches or calls. Outward heart notes in `text` remain visible text.
Conversation titles,
unknown fields and attachment metadata are not copied. Outgoing payloads are
fresh copies of immutable selected sources. Empty eligible input never calls
the injected adapter. Missing passages are marked as missing evidence.

Policy configuration, dictionaries and the entire supplied source revision
participate in selection identity. Future derived caches must verify this
identity before reuse. This prototype does not implement the cache itself.

## Integration and remaining activation gates

The archive snapshot reader, durable exclusion evidence, bounded chunks, cache
identity, reference validation, five-section publication, takeover gates and
local backup/restore are implemented. Independent reviews found and closed
privacy and integration defects, including cross-conversation closing, thinking
release evidence, provisional-tail timing and unverified pending digests.
Real adapter authorization, one real-day trial and deployment are still pending.
The existing DS dream path is retained. Do not retire client-side upload tasks
before successful, revision-bound takeover.

Tests use synthetic markers and an in-process fake adapter. No account,
subscription, network connection or production data is needed.
