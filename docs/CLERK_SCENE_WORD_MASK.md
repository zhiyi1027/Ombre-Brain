# Scene-only exclusion and word replacement

This is a local preparation path, separate from the earlier contextual/gray
experiment. It is not registered with production routes or the night scheduler.

The order is fixed:

1. Exclude every message from both speakers in confirmed intimate-scene ranges,
   including foreplay and aftercare. Ranges are half-open `[start, end)`.
2. Outside those ranges, keep every message and sentence. Replace matching words
   with exactly two literal asterisks, `**`. Do not hold neighboring messages,
   unknown prefixes or post-scene observation periods on this path.
3. Protect whitelist character spans first. Match replacement phrases longest
   first. When a replacement phrase contains a protected span, replace only its
   unprotected portions. A whitelist never rescues messages inside a scene.

`prepare_scene_redacted_day` requires an explicit `SceneManifest` bound to the
current visible-text source revision and authorized window, plus a `ClerkLexicon`.
An explicit empty range list means no confirmed scenes in that reviewed window;
missing configuration is an error. A manifest is supplied by the trusted local
caller; the word layer does not discover scene boundaries or prove a caller's
claim of confirmation. Automatic candidates from the producer below additionally
bind its detector revision and stay locked. A user-labeled trial is not automatic
detection validation. Minute-based labels must include the intended final minute, e.g.
an end-exclusive boundary at the next minute when the whole end minute is meant.

The source archive and frontend are unchanged. Only selected visible `text` is
transformed for clerk input; native thinking is never read or included in hashes.
All returned candidates remain locked against model dispatch pending integration
review. This module provides no activation or gate-bypass option.

## Private word-list configuration

Load a regular private JSON file with mode `0600` through `ClerkLexicon.load`.
There is no fallback to an absent or malformed list. Keep household words outside
Git, for example in the ignored `.night_clerk/lexicon.json` directory. Example
entries below are synthetic:

```json
{
  "schema": 1,
  "version": 1,
  "replace": ["合成替换长词", "合成替换词"],
  "whitelist": ["合成保留短语"],
  "include_core": true
}
```

`include_core` adds the existing explicit sexual-word list; whitelist protection
still wins. Terms are literal strings, never regular expressions. Single-character
entries also match within longer words. Add ordinary phrases to `whitelist` when
they should be preserved, and increment `version` when editing either list.
Load the file again for each new preparation; there is no persistent global list
cache. Source, scene ranges, word-list version/content and redacted text all bind
the selection revision, preventing stale facts or offsets from being reused.

Matching uses character-wise NFKC, strips formatting whitespace/emphasis markers,
Unicode `Cf` controls and the supported invisible variation selectors, and maps
matches back to original character positions. It does not case-fold or claim full
canonical equivalence for combining-letter sequences. Nonmatched original text
is retained. List entries and replacement matches are not diagnostic outputs.

Offsets, source hashes, fragments, evidence excerpts and displayed quotes refer
to the redacted text. The original source hash is only a local manifest binding.
Masked-text instructions tell extraction and refinement not to reconstruct `**`,
infer an event/body condition/relationship from it, or turn preserved nicknames
into real kinship. Whitelisted health words remain available as evidence; merely
mentioning a health word does not automatically create a health fact.

Legacy preparation and wire prompts are unchanged when `redaction_policy` is
absent. Existing paid caches remain intact; only exact compatible input/prompt
identities are reusable. This preview does not migrate old privacy exclusions or
automatically reuse their over-broad intervals as confirmed scene ranges.

## Automatic local scene producer

`clerk_scene_detector.prepare_automatic_scene_day` connects automatic detection
directly to the scene mask and private word replacement. It accepts the same
assembled visible snapshot, authorized window and lexicon; no human scene range
or omission checklist is a detector argument. Human labels are read only after
preparation by the evaluation script.

Strong live activity or dense interactive narration opens a scene. Bare anatomy
lists, isolated profanity, normal affection and referenced/negated/hypothetical
mentions do not open a scene by themselves. Nearby long prelude evidence can be
backfilled only after actual scene confirmation, with a ten-minute pre-roll.

Inside a scene, weak sexual cues and short continuations extend the activity
clock. Specific aftercare without real activity enters closing; the default
range ends ten minutes after the last specific care. Mixed care plus real
activity resumes the activity phase. Without care, the last activity has a
thirty-minute fallback. Generic water/goodnight does not keep a clock alive.
Expired ranges are finalized before later unrelated messages are processed, so
bedtime affection cannot reopen an old scene. Cross-conversation activity can
extend a live mask; pure care in another conversation cannot close or extend it.
An immature right boundary is clipped to the authorized window and marked open.

These are heuristic local-trial boundaries, not a promise of semantic privacy
accuracy on arbitrary text. The word layer adds no extra neighbor holds. The
producer's parameters/rules, source and window bind its `automatic_candidate`
manifest; even zero detected scenes do not grant model dispatch. This route is
not the default production preparer and does not enable scheduled nightly work.
