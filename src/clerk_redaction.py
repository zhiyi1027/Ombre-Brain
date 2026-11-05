"""Scene-only exclusion and private, literal word replacement for local previews.

Explicit source-bound ranges are required input. Automatic candidates remain
locked; their producer and trial are separate from the word replacement layer.
This module has no provider, credentials, archive-write or activation access.
"""
from dataclasses import dataclass, field, replace
from datetime import date
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from clerk_input import select_clerk_day
from clerk_privacy import (Decision, PrivacyInputError, PrivacyResult, _FORMATTING,
                           _STRONG, _aware, _digest, _messages)

VERSION = "scene-only-word-mask-v1"
NORMALIZATION_VERSION = "char-nfkc-format-cf-variation-no-casefold-v1"
MASKED_TEXT_RULE = """正文中的两个星号 ** 是程序替换的词，不代表任何已知含义。不得还原、猜测或据此提取性行为、身体异常或人物关系；其余明示的独立事实照常提取。保留的昵称也不能用来推断真实亲属关系。"""


def normalized(value):
    # Deliberately character-wise NFKC, without case folding. This gives an
    # exact monotone map back to the original visible characters.
    chars, positions = [], []
    for i, char in enumerate(value):
        for folded in unicodedata.normalize("NFKC", char):
            code = ord(folded)
            invisible = (unicodedata.category(folded) == "Cf" or code == 0x034f
                         or 0x180b <= code <= 0x180d or 0xfe00 <= code <= 0xfe0f
                         or 0xe0100 <= code <= 0xe01ef)
            if not invisible and not _FORMATTING.fullmatch(folded):
                chars.append(folded)
                positions.append(i)
    return "".join(chars), positions


def terms(values):
    if not isinstance(values, (tuple, list)) or len(values) > 2048:
        raise PrivacyInputError("invalid word list")
    output = set()
    for value in values:
        if (not isinstance(value, str) or not 1 <= len(value) <= 96
                or any(ord(c) < 32 for c in value)):
            raise PrivacyInputError("invalid word entry")
        word = normalized(value)[0]
        if not word:
            raise PrivacyInputError("empty normalized word entry")
        output.add(word)
    return tuple(sorted(output, key=lambda word: (-len(word), word)))


def pattern(words, *, overlaps=False):
    if not words:
        return None
    value = "|".join(re.escape(word) for word in words)
    return re.compile("(?=("+value+"))" if overlaps else "("+value+")")


def merge_overlaps(spans):
    output = []
    for start, end in sorted(spans):
        if output and start < output[-1][1]:
            output[-1] = (output[-1][0], max(output[-1][1], end))
        else:
            output.append((start, end))
    return output


@dataclass(frozen=True, repr=False)
class ClerkLexicon:
    version: int
    replacements: tuple = field(repr=False)
    whitelist: tuple = field(repr=False)
    include_core: bool = True

    def __post_init__(self):
        if type(self.version) is not int or self.version < 1 or type(self.include_core) is not bool:
            raise PrivacyInputError("invalid word-list version")
        object.__setattr__(self, "replacements", terms(self.replacements))
        object.__setattr__(self, "whitelist", terms(self.whitelist))

    def __repr__(self):
        return "ClerkLexicon(private_terms=True)"

    @classmethod
    def from_data(cls, value):
        if (not isinstance(value, dict) or set(value) != {"schema", "version", "replace", "whitelist", "include_core"}
                or type(value["schema"]) is not int or value["schema"] != 1):
            raise PrivacyInputError("invalid word-list schema")
        return cls(value["version"], value["replace"], value["whitelist"], value["include_core"])

    @classmethod
    def load(cls, path):
        try:
            # Open the exact private file; never search for or auto-create one.
            fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
                    raise PrivacyInputError("private word-list file required")
                data = stream.read(65537)
            def unique(pairs):
                output = {}
                for key, value in pairs:
                    if key in output:
                        raise ValueError
                    output[key] = value
                return output
            value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
        except (OSError, ValueError, UnicodeError):
            raise PrivacyInputError("private word-list file unavailable or invalid") from None
        return cls.from_data(value)

    def revision(self):
        return _digest([VERSION, NORMALIZATION_VERSION, self.version, self.replacements, self.whitelist,
                        self.include_core, _STRONG if self.include_core else ()])

    def redact(self, text):
        if not isinstance(text, str):
            raise PrivacyInputError("visible text required")
        view, positions = normalized(text)
        white = pattern(self.whitelist, overlaps=True)
        protected = merge_overlaps((positions[m.start(1)], positions[m.end(1)-1]+1)
                                  for m in white.finditer(view)) if white else []
        # The user-list limit was validated at construction. Trusted built-in
        # terms do not consume that capacity or create a late runtime failure.
        combined = set(self.replacements) | (set(_STRONG) if self.include_core else set())
        black = pattern(tuple(sorted(combined, key=lambda word: (-len(word), word))))
        matches = [(positions[m.start(1)], positions[m.end(1)-1]+1)
                   for m in black.finditer(view)] if black else []
        edits = []
        for start, end in merge_overlaps(matches):
            cursor = start
            for left, right in protected:
                if right <= cursor or left >= end:
                    continue
                if cursor < left:
                    edits.append((cursor, left))
                cursor = max(cursor, right)
            if cursor < end:
                edits.append((cursor, end))
        output, cursor = [], 0
        for start, end in edits:
            output.extend((text[cursor:start], "**"))
            cursor = end
        output.append(text[cursor:])
        return "".join(output), len(edits)


def visible_source_revision(rows):
    return _digest([[m.msg_uuid, m.conv_uuid, m.speaker, m.at.isoformat(), m.text]
                    for m in _messages(rows)])


@dataclass(frozen=True)
class SceneManifest:
    source_revision: str
    window_start: object
    window_end: object
    # Half-open ranges include all speakers/conversations. Their boundaries
    # already include foreplay/aftercare; the word layer adds no padding.
    ranges: tuple
    basis: str
    detector_revision: str | None = None

    def validated(self, source_revision, start, end):
        if (self.source_revision != source_revision or not re.fullmatch(r"[0-9a-f]{64}", self.source_revision)
                or _aware(self.window_start) != start or _aware(self.window_end) != end
                or self.basis not in ("user_confirmed", "reviewed_detector", "automatic_candidate")
                or self.basis == "automatic_candidate" and (not isinstance(self.detector_revision, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", self.detector_revision))
                or not isinstance(self.ranges, tuple) or len(self.ranges) > 1000):
            raise PrivacyInputError("confirmed scene manifest does not match source window")
        spans = []
        for span in self.ranges:
            if not isinstance(span, tuple) or len(span) != 2:
                raise PrivacyInputError("invalid confirmed scene range")
            left, right = map(_aware, span)
            if not start <= left < right <= end:
                raise PrivacyInputError("confirmed scene outside authorized window")
            spans.append((left, right))
        return tuple(merge_overlaps(spans))


def prepare_scene_redacted_day(rows, memory_day, *, window_start, window_end,
                               scenes, lexicon, snapshot_complete=False,
                               timezone_name="Asia/Shanghai", cutoff_hour=4):
    """Return a locked, redacted candidate and body-free local receipt.

    No gray propagation, unknown-prefix hold or post-scene neighbor hold exists
    on this path. Missing history is recorded, never used to invent facts.
    """
    if (snapshot_complete is not True or type(memory_day) is not date
            or type(cutoff_hour) is not int or not 0 <= cutoff_hour <= 23
            or not isinstance(scenes, SceneManifest) or not isinstance(lexicon, ClerkLexicon)):
        raise PrivacyInputError("confirmed scenes and private word list required")
    start, end = _aware(window_start), _aware(window_end)
    if start >= end:
        raise PrivacyInputError("invalid authorized window")
    rows = tuple(rows)
    messages = _messages(rows)
    if any(m.at < start or m.at >= end for m in messages):
        raise PrivacyInputError("source outside authorized window")
    source_revision = visible_source_revision(rows)
    spans = scenes.validated(source_revision, start, end)
    decisions = tuple(Decision(m.msg_uuid, m.at,
        "intentional_skip" if any(a <= m.at < b for a, b in spans) else "allowed",
        "confirmed_private_scene" if any(a <= m.at < b for a, b in spans) else "outside_confirmed_scene")
        for m in messages)
    policy_revision = _digest([VERSION, lexicon.revision(), start.isoformat(), end.isoformat(),
                               scenes.basis, scenes.detector_revision,
                               [[a.isoformat(), b.isoformat()] for a, b in spans]])
    result = PrivacyResult(messages, decisions, (), policy_revision, source_revision,
                           _digest([source_revision, policy_revision]), VERSION)
    day = select_clerk_day(result, memory_day, timezone_name=timezone_name, cutoff_hour=cutoff_hour)
    sanitized, edit_count, changed_messages = [], 0, 0
    for source in day.messages:
        text, count = lexicon.redact(source.text)
        sanitized.append(replace(source, text=text))
        edit_count += count
        changed_messages += text != source.text
    day = replace(day, messages=tuple(sanitized), context_complete=False,
                  privacy_review_required=True, redaction_policy=lexicon.revision(),
                  selection_revision=_digest([day.selection_revision, VERSION, lexicon.revision(),
                                              [[s.source_id, s.text] for s in sanitized]]))
    receipt = {"profile": VERSION, "scene_basis": scenes.basis, "range_count": len(spans),
               "spoken_messages": len(day.coverage), "scene_skipped": sum(d.status != "allowed" for d in day.coverage),
               "retained_messages": len(day.messages), "changed_messages": changed_messages,
               "replacement_spans": edit_count, "lexicon_revision": lexicon.revision(),
               "privacy_review_required": True, "context_complete": False}
    return day, receipt
