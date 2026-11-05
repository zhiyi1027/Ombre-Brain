"""Automatic scene ranges for the locked scene-only/word-mask local trial.

No human range, facts checklist, private vocabulary or native thinking is an
input to detection. Bare sensitive words outside a scene remain word-mask work.
"""

from dataclasses import dataclass
from datetime import timedelta
import re

from clerk_privacy import PrivacyInputError, _STRONG, _aware, _digest, _messages
from clerk_redaction import NORMALIZATION_VERSION, SceneManifest, normalized, prepare_scene_redacted_day

DETECTOR_VERSION = "automatic-intimate-scene-v1"
RISK_FAMILIES = {
    "oral": ("舌尖", "含住"),
    "breathing": ("喘息", "呻吟", "低喘"),
    "lower_body": ("腿间", "大腿内侧"),
    "undressing": ("褪下", "脱掉"),
    "touch": ("抚摸",),
    "arousal": ("欲望", "硬挺", "湿润", "颤抖"),
}
SPECIFIC_CARE = (
    "热毛巾",
    "帮你清理",
    "清理干净",
    "擦干净",
    "擦好了",
    "擦好",
    "擦完",
    "疼不疼",
    "还疼",
    "不舒服吗",
    "抱着睡",
    "抱好",
)
CONTACT_ACTION = r"舔|吸吮|揉捏|抽动|插入|进入|顶进|磨蹭|摩擦|套弄|含住|撸|握住|捏住"
PRESENT_ACTIVITY = r"(?:正在|现在|开始|继续|在).{0,12}(?:做爱|口交|性交|抽插|射精)"
INTERACTION = r"舔|吸吮|揉|捏|摸|抽动|插入|顶|磨|摩擦|含|握|夹"
DISCUSSION = r"词表|词汇表|解剖学|医学说明|科普|教材|术语|下面列出|定义为"
CONTINUATION = r"^(?:嗯+|好+|啊+|唔+|继续|再来|还要|慢点|快点|不要停|抱紧我)[。！？!?…～]*$"
RESUME = r"^(?:继续|再来|还要|不要停)[。！？!?…～]*$"
REFERENCE_QUOTES = r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"'
NON_LIVE_REFERENCE = r"没有|没在|不在|并非|不是|假如|如果|要是|假设|比如|例如|想象|幻想|假装|昨天|昨晚|前天|上次|之前|过去|以前|曾经|回忆|梦里|梦见|电影里|故事里|小说里|文中|这句话|这一句"
CLAUSE_BOUNDARIES = r"[。！？?!；;]|但是|不过|可是"


@dataclass(frozen=True)
class SceneDetectionPolicy:
    pre_roll: timedelta = timedelta(minutes=10)
    no_signal_tail: timedelta = timedelta(minutes=30)
    care_tail: timedelta = timedelta(minutes=10)
    long_chars: int = 300
    long_distinct_cues: int = 2
    dense_distinct_cues: int = 3

    def __post_init__(self):
        if any(
            not isinstance(v, timedelta) or v <= timedelta(0)
            for v in (self.pre_roll, self.no_signal_tail, self.care_tail)
        ) or any(
            type(v) is not int or v < minimum
            for v, minimum in ((self.long_chars, 1), (self.long_distinct_cues, 2), (self.dense_distinct_cues, 2))
        ):
            raise PrivacyInputError("invalid automatic scene policy")

    def revision(self):
        values = {k: v.total_seconds() if isinstance(v, timedelta) else v for k, v in vars(self).items()}
        return _digest(
            [
                DETECTOR_VERSION,
                NORMALIZATION_VERSION,
                values,
                _STRONG,
                RISK_FAMILIES,
                SPECIFIC_CARE,
                CONTACT_ACTION,
                PRESENT_ACTIVITY,
                INTERACTION,
                DISCUSSION,
                CONTINUATION,
                RESUME,
                REFERENCE_QUOTES,
                NON_LIVE_REFERENCE,
                CLAUSE_BOUNDARIES,
            ]
        )


def scene_features(text, policy):
    raw = normalized(text)[0]
    unquoted = re.sub(REFERENCE_QUOTES, " ", raw)
    # A comma cannot erase a past/conditional/negative qualifier. An explicit
    # contrast may start a fresh scope, preserving a subsequent live action.
    content = ";".join(
        part for part in re.split(CLAUSE_BOUNDARIES, unquoted) if not re.search(NON_LIVE_REFERENCE, part)
    )
    explicit = frozenset(word for word in _STRONG if word in content)
    families = frozenset(name for name, words in RISK_FAMILIES.items() if any(w in content for w in words))
    care = any(w in content for w in SPECIFIC_CARE)
    contact = bool(re.search(CONTACT_ACTION, content))
    hard_action = bool(
        re.search(PRESENT_ACTIVITY, content)
        or contact
        and (any(w in content for w in _STRONG[7:]) or len(families) >= 2)
    )
    kinds = len(explicit) + len(families)
    interaction = bool(re.search(INTERACTION, content))
    discussion = bool(re.search(DISCUSSION, content))
    strong = not discussion and (
        hard_action
        or len(families) >= policy.dense_distinct_cues
        or interaction
        and (
            len(explicit) >= policy.dense_distinct_cues
            or len(content) >= policy.long_chars
            and kinds >= policy.long_distinct_cues
        )
    )
    # Anatomy named during cleaning/checking is not, on its own, a new act.
    activity = hard_action or len(families) >= policy.dense_distinct_cues and interaction
    pure_care = care and not activity
    opening = strong and not pure_care
    return {
        "opening": opening,
        "hard_action": hard_action,
        "care": care,
        "sensitive": bool(explicit or families),
        "length": len(content),
        "explicit_types": len(explicit),
        "risk_families": len(families),
        "pure_care": pure_care,
        "activity": activity,
        "continuation": bool(re.fullmatch(CONTINUATION, raw)),
        "resume": bool(re.fullmatch(RESUME, raw)),
        "prelude": not discussion
        and not care
        and len(content) >= policy.long_chars
        and kinds >= policy.long_distinct_cues,
    }


def detect_scene_ranges(rows, *, window_start, window_end, snapshot_complete=False, policy=None):
    if snapshot_complete is not True:
        raise PrivacyInputError("complete assembled visible snapshot required")
    policy = policy or SceneDetectionPolicy()
    if not isinstance(policy, SceneDetectionPolicy):
        raise PrivacyInputError("invalid automatic scene policy")
    start, end = _aware(window_start), _aware(window_end)
    if start >= end:
        raise PrivacyInputError("invalid automatic scene window")
    rows = tuple(rows)
    messages = _messages(rows)
    if any(m.at < start or m.at >= end for m in messages):
        raise PrivacyInputError("source outside authorized window")
    active, finished, preludes = {}, [], {}

    def deadline(scene):
        return (
            scene["last_care"] + policy.care_tail
            if scene["phase"] == "care"
            else scene["last_activity"] + policy.no_signal_tail
        )

    def close(cid, scene, at, reason):
        finished.append(
            {
                "start": scene["start"],
                "end": min(at, end),
                "opened_at": scene["opened_at"],
                "reason": reason,
                "saw_aftercare": scene["saw_aftercare"],
            }
        )
        active.pop(cid, None)

    for m in messages:
        # Expiry is settled before unrelated future chatter is inspected. A
        # bedtime care word hours later cannot reopen the old scene.
        for cid, scene in tuple(active.items()):
            until = deadline(scene)
            if m.at > until:
                close(cid, scene, until, "aftercare_tail" if scene["phase"] == "care" else "no_signal_fallback")
        f = scene_features(m.text, policy)
        hints = [at for at in preludes.get(m.conv_uuid, []) if m.at - at <= policy.pre_roll]
        if f["prelude"]:
            hints.append(m.at)
        preludes[m.conv_uuid] = hints
        for cid, other in active.items():
            if cid != m.conv_uuid and f["sensitive"] and not f["pure_care"]:
                other.update(last_activity=m.at, last_care=None, phase="active")
        scene = active.get(m.conv_uuid)
        if scene is None:
            if not f["opening"]:
                continue
            first = min(hints + [m.at])
            active[m.conv_uuid] = {
                "start": max(start, first - policy.pre_roll),
                "opened_at": m.at,
                "last_activity": m.at,
                "last_care": None,
                "phase": "active",
                "saw_aftercare": False,
            }
        elif f["activity"] or f["resume"]:
            scene.update(last_activity=m.at, last_care=None, phase="active")
        elif f["care"]:
            scene.update(last_care=m.at, phase="care", saw_aftercare=True)
        elif f["opening"]:
            scene.update(last_activity=m.at, last_care=None, phase="active")
        elif f["sensitive"] and scene["phase"] == "active":
            scene["last_activity"] = m.at
        elif f["continuation"] and scene["phase"] == "active":
            scene["last_activity"] = m.at
        # Isolated mentions in care and generic water/goodnight do not restart
        # an activity clock. The whole current scene still covers those turns.
    for cid, scene in tuple(active.items()):
        until = deadline(scene)
        reason = (
            "window_open_scene"
            if until >= end
            else "aftercare_tail"
            if scene["phase"] == "care"
            else "no_signal_fallback"
        )
        close(cid, scene, until, reason)
    finished.sort(key=lambda s: (s["start"], s["end"]))
    source_revision = _digest([[m.msg_uuid, m.conv_uuid, m.speaker, m.at.isoformat(), m.text] for m in messages])
    manifest = SceneManifest(
        source_revision,
        start,
        end,
        tuple((s["start"], s["end"]) for s in finished),
        "automatic_candidate",
        policy.revision(),
    )
    receipt = {
        "detector": DETECTOR_VERSION,
        "detector_revision": policy.revision(),
        "detected_ranges": len(finished),
        "human_ranges_used": False,
        "scenes": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in s.items()} for s in finished],
    }
    return manifest, receipt


def prepare_automatic_scene_day(
    rows,
    memory_day,
    *,
    window_start,
    window_end,
    lexicon,
    snapshot_complete=False,
    policy=None,
    timezone_name="Asia/Shanghai",
    cutoff_hour=4,
):
    rows = tuple(rows)
    scenes, detection = detect_scene_ranges(
        rows, window_start=window_start, window_end=window_end, snapshot_complete=snapshot_complete, policy=policy
    )
    day, receipt = prepare_scene_redacted_day(
        rows,
        memory_day,
        window_start=window_start,
        window_end=window_end,
        scenes=scenes,
        lexicon=lexicon,
        snapshot_complete=snapshot_complete,
        timezone_name=timezone_name,
        cutoff_hour=cutoff_hour,
    )
    return day, {**receipt, **detection}
