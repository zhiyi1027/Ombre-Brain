# Raw night clerk — local candidate, disabled by default

## Reviewed production input

Real subscription configuration must explicitly choose
`night_clerk.input_profile=scene_word_mask`, a private `lexicon_path`, and an
`approved_input_revision` computed by `clerk_production.approval_revision` for
that exact lexicon, detector and day-window policy. Missing files or mismatched
approval stop processing; they never fall back to the legacy input. Generic
preview helpers remain locked. The production owner is separately tested and
reviewed, rather than copying a one-day preview's gate override.

The owner reads the assembled visible-text prefix through a fixed boundary:
the next 04:00 Shanghai cutoff plus one hour for local scene-boundary context.
It cannot dispatch before 05:00. Both model input and publication contain only
the selected memory day. Later days or advancing wall time do not churn an old
day's input revision. Cross-cutoff scenes and later pre-roll are included in
local filtering. Pending fragments or visible-history gaps require review and
stop the run. Native thinking is never part of this snapshot.

Automatic work additionally requires an explicit `automatic_start_day`; startup
does not silently backfill the previous seven days. A polling tick attempts at
most one new day, skipping already-current or paused days. Any failed prepared
revision pauses durably across restarts. An authenticated manual run is an
explicit retry, but cannot reset `max_calls_per_day` (default 64), which counts
received, failed and unknown calls across all retries. Caches still revalidate
and remain reusable. Quota or protocol errors must not create a five-minute
subscription-spending loop.

Code deployment, private configuration, account ownership transfer, production
generation and revision-bound Breath takeover remain separate steps. Keep
automatic work off until the actual deployment is checked. Fact cards remain
in the private clerk database; there is no new automatic deletion schedule.

The new path is wired through `DailyContinuityService`, the authenticated
daily-continuity routes and Breath, behind `night_clerk.enabled=false`.
`automatic=false` is a separate gate. The subscription adapter requires its own
explicit account authorization and configuration; turning on the flag alone
does not authorize or connect an account. See [CHATGPT_SUBSCRIPTION.md](CHATGPT_SUBSCRIPTION.md).
An unavailable adapter never falls back to DS or a paid API.

## Responsibilities and evidence

DS tagging and its existing night-dream path are retained. The separate clerk
adapter extracts facts from original transcripts only. It cannot read buckets,
feel, plan, notes or dreams, and has no plan writer or executable tools.

The raw archive supplies one complete assembled timeline. Pending fragments,
invalid timestamps or conflicting revisions block generation. Local privacy
selection runs before day selection, splitting or any model call. A memory day
uses Asia/Shanghai and changes at 04:00 by default.

Privacy start markers and excluded message IDs persist separately from content.
Hidden, withdrawn, replaced or purged source positions leave body-free gap
signals. Incomplete fragments' claimed digests are not trusted as proof that
discarded text was retained. Verified equivalent legacy reassembly does not
create spurious gaps and never clears an existing gap.

Keyword recognition remains a heuristic, with intentionally conservative holds.
Synthetic tests and review do not measure real recall or establish zero leaks.
The full snapshot scan favors correctness over incremental performance; its
cost on a real multi-day archive is not yet measured.

## Processing, recovery and usage

Allowed original text is split into traceable fragments with source hashes and
character offsets. Long messages are never silently truncated. Each request
stays within a configurable conservative UTF-8 byte bound (initially 16000),
including instructions and JSON overhead. This bound is not measured model
token consumption.

Map proposals cite only fragments actually sent. Body, food and mood require
the user's source evidence. Reserved quotation labels, multiline structure,
unknown references and unsupported dates reject the response. A separately
versioned evidence-review stage checks cached map proposals against only their
existing source excerpts. It can correct unsupported wording, reclassify and
merge related proposals, or explicitly drop them. Every input proposal is
accounted for exactly once; each merged proposal contributes cited evidence.
This is not a guarantee of semantic completeness: a mixed proposal currently
keeps its principal supported fact rather than being split into several cards.
Candidates cannot become facts, ordinary facts cannot become candidates, and
dates cannot be invented or resolved by merging conflicting candidates.

Reviewed cards carry `scope=day/past/preference`. Past events and long-term
preferences remain searchable, but do not enter daily selection or section
preservation. Hunger and sleepiness are not inferred emotions; ordering food
does not prove it was eaten. Grey's narration is not independent image evidence.
The review still depends on a fallible model; exact references alone do not
prove a faithful paraphrase.

Hierarchical selection returns existing reviewed IDs. It also groups every
event candidate, including candidates from different review batches, by
underlying event. Local validation enforces at most one selected card per
reviewed event group and section caps, trimming only valid existing IDs by
importance and stable model order. Invalid groups, unknown/duplicate IDs and
total-limit violations still fail closed. Semantic grouping itself remains
subject to quality review. The five sections are events, undecided matters,
body, food and mood.

The selector sees short local labels and an explicit list of event labels.
A digest of the original card-ID order binds the label mapping into the cache
key; accepted labels resolve back to the original cards before publication.
This reduces copying errors without accepting unknown labels or incomplete
event groups.
Empty sections say there is no displayed entry, without claiming nothing
happened or that health/meals were normal. Up to two short original sentences
are pasted by the program from the full source with verified sentence bounds;
the mood section has priority when its evidence supplies a suitable sentence.
Quote display uses the configured local timezone (Shanghai by default), while
source timestamps remain unchanged.
Selection cannot erase an entire kind present in the validated fact cards:
missing kinds regain one existing representative from the reviewed daily pool,
including cards lost at intermediate selection stages. Rendering first removes
extra cards from repeated kinds. If a single remaining card still cannot fit,
that section says a fact card exists but was not expanded within the budget.
The original card remains searchable; absence of display never becomes absence
of evidence. The publication render version changes without invalidating the
already validated extraction caches. Review and selection instructions have
separate identities, so correcting a later stage need not repeat map requests.

Chunks cache by actual fragment content, policy, instructions and adapter/model
identity. Every cached response is revalidated. Per-request durable usage
records survive failures and retries; failed requests with unknown consumption
remain marked unknown. A successful atomic publication includes the cumulative
ledger. Cancellation, timeout, quota failure or bad structure leave work pending;
they do not replace the last successful publication. Changed or hidden sources
make old publications unavailable until regenerated.

A renewable SQLite lease prevents parallel generation; the publication
transaction verifies owner and expiry. Model/provider/prompt changes invalidate
the publication identity. Source selection is checked before calls and before
publication. Breath checks that same publication again after asynchronous
rendering; stale output is discarded.

## Fact cards and plans

### Additional quality passes for scene/word-masked input

The masked input path adds a separately versioned family-quality stage after
cached extraction and evidence review. It scans the user's sentences alone for
affection directed at Grey; the model returns only unit IDs and fixed categories.
Each selected sentence becomes its own mood card. `verbatim` is copied by the
program from the sanitized source with exact offsets, separately from the plain
summary; neither the model nor ordinary fact summaries can author direct quotes.
Conservative local positives supplement the scan, while ambiguous subjects,
negation, quotation, hypothetical statements and action wishes require semantic
classification. Original wording is also available to fact search and selection.
Positive labels require the current sentence itself to express affection;
acknowledgements, care requests and incomplete utterances are not upgraded into
love or longing. The wire version and eligibility-rule version are separate,
so a stricter mood prompt need not discard an unchanged follow-up request cache.
An optional private `night_clerk.self_reference_aliases` list can bind explicit
first-person self-references. Aliases are escaped as literal grammar tokens;
the complete subject and allowed sentence ending must match. An omitted clear
sentence may then be restored from the same raw unit, marked
`local_affect_rule=explicit_first_person`. The profile affects publication
identity and is not appended to model inputs; alias text already present in
approved source sentences can still be sent as ordinary source or verbatim text.

Open questions are reviewed individually, including multiple questions within
one source message. Each gets only the next three original spoken positions in
the same known conversation; gaps, intentional skips and conversation changes
stop the window. Unknown conversation metadata holds the question as uncertain.
An answered decision must cite an allowed following message from the other
participant. IDs prove provenance, not that an answer is semantically adequate.
Answered and uncertain units leave the displayed open-question pool; a still
unanswered unit retains its own evidence. Immediate actions leave the commitment
candidate pool without becoming completed facts; future commitments remain
subject to explicit confirmation before entering plan.
Local action exclusions require both the candidate summary and an affirmative,
unquoted evidence clause to match. A same-day suggestion to discuss something
after waking is not a durable candidate. An unrelated action in the same long
excerpt cannot cancel another promise; explicit future or repeated commitments
remain protected from that local rule.

Hard impression priority is health, daily outings/activities/purchases/food,
the user's affection toward Grey, other matters, and finally chores such as
shipping or device setup. Event grouping keeps route, purchase and chore classes
separate. All validated event unions are retained across selection depths, and
the final ranking revisits the complete card pool so earlier thinning cannot
erase higher-priority cards. Rendering removes lower-priority cards first within
the existing 1000-character/900-estimated-token limit. These quality operations
have strict wire projections and participate in the existing call limits,
cache validation, source checks and publication identity. Unmasked legacy wire
contracts remain unchanged. This does not activate production or establish
real-day quality acceptance.

Fact cards persist across days in `.night_clerk/clerk.db`, outside ordinary
buckets, embeddings and Git sync. Cards carry stable derived IDs, kind, summary,
importance, scope, reviewed-map lineage, source message IDs/hashes/times and character offsets. Search uses
date and literal keywords. There is no new semantic embedding service or
automatic policy of permanent retention. Retention must be decided before
real activation; original evidence remains authoritative.

Use `breath_search(domain="fact", query="...")` for current cards, or
`domain="commitment_candidate"` for candidates. Responses obey the requested
whole-response token budget. The authenticated `/api/night-clerk/facts` endpoint
also accepts `day`, `query`, `limit` and `candidates=true`. Original evidence can
be checked against its message ID and time in the existing raw archive view.

Candidates never enter formal plan automatically. Explicit `plan()` calls may
provide `due_date=YYYY-MM-DD`; a candidate additionally requires its ID and
`confirmed_by` equal to 知知 or 顾凛. Empty or legacy dates remain undated.
The plan action API supports explicit `reschedule`, recording the old and new
date. New clerk mode disables the older DS auto-completion heuristic.

## Breath and takeover

New layout: core rules → unresolved quarrel → yesterday → dated plan → up to
three recent buckets → one eligible old memory and one feel. Dreams remain an
independent section. Today plus the following six Shanghai calendar days are
expanded; overdue dates are labeled and far-future plans get pointers.
Exact duplicate factual display is suppressed; related/independent feel is not
deleted or suppressed. Semantic paraphrase dedup is not implemented.

The complete envelope shares the existing cap, including all section headers,
pointers, dreams and feelings. Whole items become pointers when they cannot fit.

Notes stop accepting uploads only after a current successful clerk day was
actually returned in Breath and a human/顾凛 explicitly confirms that exact
`expected_revision` at `/api/night-clerk/takeover`. Until then, the old uploader
may still deliver notes. The retired uploader stub must not be installed on the
existing client before takeover. Afterwards upload handlers authenticate and
return 410 before reading a body. Old notes remain readable archives; they never
feed the new clerk.

## Backup and remaining real-world gates

The separate authenticated `/api/night-clerk/backup` bundle covers the raw
SQLite database, privacy state, caches, cards and publications with hashes.
Restore verifies members/hashes/SQLite and only creates a new empty root;
leases and takeover receipts reset. It contains no account/session/config
files. Existing Markdown/GitHub backup alone does not cover these databases.
Any real bundle is private data and must be handled accordingly.

Local tests and code review do not imply real quality or activation.
Account authorization, synthetic inference, specifically authorized real-day
quality and usage checks, retention choice, production deployment and client
handoff are separate gates. Night-dream refinements (balanced material, substantive
old memories, a desire block and concrete scenes) remain a later task as agreed.

## Thinking is viewable archive data, never clerk evidence

The archive retains both structured `text` and native `thinking`, including
v2 fragments and their original `[text, thinking]` transport digest. The
Dashboard can display/search thinking when its checkbox is selected; explicit
`raw_browse(thinking=True)` remains available. Full raw/clerk recovery backups
preserve the archive fields. This does not send archive data to a model.

The clerk's separate snapshot selects only visible `text` columns. Native
thinking cannot enter privacy classification, source/cache identities, fact
cards, quotes, yesterday's impression or model requests. Public heart notes in
`text` remain spoken evidence; independent feel keeps its existing role. A
thinking-only turn is not a spoken turn and creates no clerk gap on deletion.
Completed thinking-only revisions do not invalidate clerk publications.
Transport checksums authenticate both fields but never identify clerk sources.

Pending mixed fragments block processing until the full message is assembled:
missing fragments could still contain spoken text. Unknown-origin legacy
exclusion metadata requires visible-text revalidation; it is never silently
trusted or erased. No text-only sender migration is required by this version.
The actual client and production service have not been changed or deployed.
