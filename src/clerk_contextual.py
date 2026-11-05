"""Opt-in contextual privacy profile, evaluated locally before activation.

Affection is not a scene clock. An established scene still masks both speakers
until a sustained, user-supported ordinary transition has been observed.
"""
from datetime import timedelta
import re
import unicodedata

from clerk_privacy import (Decision, Interval, PositiveMarker, PrivacyInputError,
                           PrivacyResult, _FORMATTING, _ID, _STRONG, _aware,
                           _digest, _mask_intervals, _messages)

CONTEXT_VERSION = "clerk-contextual-v10"
RISK_FAMILIES = {
    "mouth": ("舌尖", "含住"),
    "breathing": ("喘息", "呻吟", "低喘"),
    "lower_body": ("腿间", "大腿内侧"),
    "undressing": ("褪下", "脱掉"),
    "touch": ("抚摸",),
    "arousal": ("欲望", "硬挺", "湿润", "颤抖"),
}
AFFECTION = ("亲吻", "吻", "亲亲", "抱抱", "拥抱")
SPECIFIC_CARE = ("热毛巾", "疼不疼", "还疼", "抱着睡", "抱好", "不舒服吗", "累不累", "帮你清理")
GENERIC_CARE = ("毛巾", "擦", "喝水", "晚安", "清理", "睡一会")
TOPICS = {
    "work": r"上班|下班|开会|代码|测试|文件|课程|作业|备课",
    "meal": r"早餐|午饭|晚饭|点餐|外卖|餐馆|餐厅|(?:点了|吃了|吃完|买了).{0,20}(?:饭|面|粥|饼|汤|饮料|奶茶|咖啡)|(?:饿了|吃饱).{0,10}(?:饭|面|粥|饼)",
    "health": r"(?:眼睛|双眼|红眼|眼药|结膜炎|喉咙|体温|头疼|头痛|发烧|咳嗽|感冒|挂号|门诊|药店)",
    "travel": r"等车|打车|公交|机票|司机|(?:到|回|订).{0,6}酒店|景区|景点|商业街|商业小街",
    "shopping": r"超市|快递|订单|结账|(?:买了|挑了|看中).{0,20}(?:衣服|玩偶|盲盒|积木|礼物|文具|东西|周边|零食)|洗衣服|擦桌子",
}
PREDICATES = {
    "work": r"(?:文件|代码|测试|任务|作业|会议).{0,20}(?:通过|完成|结束|改好|写好|交了|发了|修好)|(?:已经|现在|刚|我).{0,8}(?:到公司|上班了|下班了|开完会|备完课)",
    "meal": r"点了|点餐|买了|吃了|喝了|吃完|喝完|吃饱|外卖到了|餐到了|送到了",
    "health": r"(?:眼药|药水|药膏).{0,8}(?:用|滴|涂)|(?:用|滴|涂).{0,8}(?:眼药|药水|药膏)|结膜炎|发烧了|体温.{0,8}度|看医生|看了医生|挂号了|挂了号|去了医院|到医院了|药店.{0,8}买药",
    "travel": r"到了|到达|回到|回来了|等车|叫了车|打到车|上车了|下车了|车来了|订了|买了",
    "shopping": r"买了|买到|结账|付了|付款|收到了|到了|洗好了|擦完|擦好|挑了|选了",
}
NON_ACTUAL = r"如果|假如|要是|想象|幻想|假装|仿佛|明天|后天|打算|想去|想要|要不要|会不会|好像|但愿|希望|如同|昨天|前天|上次|以前|当时|那天|去年|上周|小时候|曾经|故事里|剧里|梦到|想起"
CONTACT_ACTION = r"舔|吸吮|揉捏|抽动|插入|进入|顶进|磨蹭|摩擦|套弄|含住|撸|握住|捏住"
HEALTH_STATE = r"(?:眼睛|双眼).{0,10}(?:红|痒|肿|不疼|不痛)|(?:我|现在|成|又).{0,10}红眼|红眼.{0,8}(?:了|啦|呀)"
ACTUAL_PRIVATE = r"(?:正在|现在|开始|继续|在).{0,12}(?:做爱|口交|性交|抽插|射精)"
INDEPENDENT_AFFECTION = r"喜欢你|爱你|想你|最喜欢|亲亲|抱抱|变成|我叫|叫我|最好|真好|好哄|好喂"
DAILY_REFERENTS = r"眼睛|眼药|红眼|午饭|晚饭|外卖|点餐|睡衣|盲盒|积木|公仔|周边|商业街|商业小街|景区|快递|订单|文件"
AMBIGUOUS_REPLY = r"再来|继续|还要|要你|受不了|里面|深点|快点|慢点|这里|那里|刚才|身体|欲|湿|硬|这样|那样|这么|那么"
TRANSITION_DOMAINS = {
    "meal": r"早餐|午饭|晚饭|外卖|餐馆|餐厅|菜单|菜品|小吃|奶茶|咖啡|面条|炒面|米饭|粥|饼|汤",
    "shopping": r"衣服|睡衣|盲盒|玩偶|积木|商品|快递|订单|商店|周边|礼物|尺码|颜色|价格|公仔",
    "travel": r"景区|景点|商业街|商业小街|门票|路线|酒店|城墙|日落|公交|出租车|商场|街区|舞台",
    "work": TOPICS["work"],
}
TRANSITION_PREDICATE = r"什么|怎么|哪里|哪个|哪家|几点|多少|是不是|有没有|还是|[吗呢么]|我(?:感觉|觉得|认为|准备|打算)|点了|吃了|买了|到了|看中|选了|完成|通过"
EXTERNAL_ACTION = {
    "meal": r"吃|喝|点|选|买|菜单",
    "shopping": r"买|选|挑|价格|多少钱|颜色|尺码|尺寸|到货|寄|退货|付款|质量|材质|哪家店",
    "travel": r"我(?:感觉|觉得|认为)|去|到|回|几点|关门|开门|门票|路线|怎么走|多远|多久|在哪",
    "work": r"写|改|交|完成|通过|安排|几点|怎么|哪个|哪份",
}
PERSON_AS_OBJECT = r"(?:你|我)(?:就是|是不是|是|像|仿佛|好像|变成|成了)(?:我|你|我们)?(?:的|最喜欢的|最爱的)?(?:一[个件份只]|那个|这个)?(?:礼物|盲盒|午饭|晚饭|餐厅|景区|景点|文件|衣服|玩偶|公仔|商品)"
TRANSITION_VETO = r'[“”"「」]|比如|例如|举例|这句话|想象|幻想|假装|仿佛|如同|如果|假如|要是|梦|故事|剧里|昨天|以前|上次|回忆|曾经|脱|床|被窝|腰|腿|胸|手指|皮肤|舌|喘|舔|揉|抚|颤|你.{0,8}(?:心|眼)'


def policy_terms():
    return [RISK_FAMILIES, AFFECTION, SPECIFIC_CARE, GENERIC_CARE, TOPICS, PREDICATES, NON_ACTUAL, CONTACT_ACTION, HEALTH_STATE,
            ACTUAL_PRIVATE, INDEPENDENT_AFFECTION, DAILY_REFERENTS, AMBIGUOUS_REPLY,
            TRANSITION_DOMAINS, TRANSITION_PREDICATE, TRANSITION_VETO, EXTERNAL_ACTION, PERSON_AS_OBJECT]


def longest_hits(content, words):
    """One character span cannot count as both a long word and its suffix."""
    pattern = "|".join(re.escape(w) for w in sorted(set(words), key=lambda w: (-len(w), w)))
    return tuple(m.group() for m in re.finditer(pattern, content)) if pattern else ()


def features(text):
    content = _FORMATTING.sub("", unicodedata.normalize("NFKC", text))
    words = {word: family for family, values in RISK_FAMILIES.items() for word in values}
    hits = longest_hits(content, words)
    families = frozenset(words[word] for word in hits)
    topics = frozenset(name for name, pattern in TOPICS.items() if re.search(pattern, content))
    specific_care = any(word in content for word in SPECIFIC_CARE)
    explicit = any(word in content for word in _STRONG)
    anchor = False
    actual_action_anchor = False
    # A noun or question alone is not an independently stated ordinary fact.
    # Specific care is never cancelled merely by appending a daily-topic word.
    for match in re.finditer(r"([^。！？?；\n]+)([。！？?；\n]|$)", content):
        sentence, ending = match.groups()
        # A comma does not erase a future/hypothetical/past qualifier.
        if ending in ("?", "？") or sentence.endswith(("吗", "么")) or re.search(NON_ACTUAL, sentence):
            continue
        for clause in re.split("[，,]", sentence):
            if any(len(clause) >= (3 if name == "health" else 4)
                   and re.search(pattern, clause) and re.search(PREDICATES[name], clause)
                   for name, pattern in TOPICS.items()):
                anchor = True
                actual_action_anchor = True
            elif re.search(HEALTH_STATE, clause):
                anchor = True
    ambiguous = bool(re.search(AMBIGUOUS_REPLY, content))
    anchor = anchor and not (explicit or families or specific_care or ambiguous)
    explicit_words = longest_hits(content, _STRONG)
    anatomy = any(word in content for word in _STRONG[7:])
    independent = (len(content) >= 4 and not (explicit or families or specific_care)
        and not ambiguous
        and not re.search(r"你.{0,5}(?:心|眼|身体).{0,8}(?:像|仿佛)", content)
        and bool(re.search(INDEPENDENT_AFFECTION, content) or re.search(DAILY_REFERENTS, content)))
    transition_topics = frozenset()
    if (len(content) >= 6 and not (explicit or families or specific_care or ambiguous)
            and not re.search(TRANSITION_VETO, content)
            and not re.search(PERSON_AS_OBJECT, content)
            and not any(word in content for word in GENERIC_CARE)
            and re.search(TRANSITION_PREDICATE, content)):
        transition_topics = frozenset(name for name, pattern in TRANSITION_DOMAINS.items()
                                      if re.search(pattern, content) and re.search(EXTERNAL_ACTION[name], content))
    return {"explicit": explicit, "families": families,
            "affection": bool(longest_hits(content, AFFECTION)), "topics": topics,
            "ordinary_anchor": anchor, "specific_care": specific_care,
            "actual_action_anchor": actual_action_anchor and anchor,
            "explicit_types": len(set(explicit_words)),
            "direct_action": any(word in content for word in _STRONG[:7]),
            "contact_action": anatomy and bool(re.search(CONTACT_ACTION, content)),
            "actual_private": bool(re.search(ACTUAL_PRIVATE, content)),
            "independent_statement": independent,
            "transition_topics": transition_topics,
            "generic_care": not anchor and any(word in content for word in GENERIC_CARE),
            "length": len(content)}


def topic_pair(proof, at, policy):
    """Two same-topic user utterances, not two asserted historical facts."""
    recent = [(t, topics) for t, topics in proof if at-t <= policy.rolling_window]
    proof[:] = recent
    if not recent:
        return None
    last_at, last_topics = recent[-1]
    return next((t for t, topics in recent[:-1]
                 if last_at-t >= policy.ordinary_span and topics & last_topics), None)


def filter_contextual(rows, *, as_of, policy, positive_markers=(), withheld_ids=()):
    now = _aware(as_of)
    messages = _messages(rows)
    withheld = frozenset(withheld_ids)
    if any(not isinstance(mid, str) or not _ID.fullmatch(mid) for mid in withheld):
        raise PrivacyInputError("invalid prior exclusion IDs")
    markers = set()
    for value in positive_markers:
        marker = value if isinstance(value, PositiveMarker) else PositiveMarker(_aware(value))
        if marker.conv_uuid is not None and not _ID.fullmatch(marker.conv_uuid):
            raise PrivacyInputError("invalid positive marker scope")
        markers.add((_aware(marker.at), marker.conv_uuid))
    markers = tuple(sorted(markers, key=lambda item: (item[0], item[1] or "")))
    events = [(m.at, 1, m.msg_uuid, m) for m in messages]
    events += [(at, 0, cid or "", None) for at, cid in markers]
    events.sort(key=lambda event: event[:3])
    scenes, active, closed, rolling, content_bands, discussions = [], {}, {}, {}, [], {}
    discussion_segments = []
    for at, _, scope, message in events:
        cid = message.conv_uuid if message else scope or None
        f = features(message.text) if message else {"explicit": True, "families": frozenset(),
              "affection": False, "topics": frozenset(), "specific_care": False,
              "ordinary_anchor": False, "actual_action_anchor": False, "generic_care": False,
              "direct_action": True, "contact_action": False, "actual_private": True,
              "independent_statement": False, "transition_topics": frozenset(), "explicit_types": 0, "length": 0}
        start_grade = (message is None or f["actual_private"] or f["contact_action"]
                       or f["explicit_types"] >= policy.dense_hits
                       or f["length"] >= policy.long_chars and f["explicit_types"]+len(f["families"]) >= 2)
        chain = [(t, values) for t, values in rolling.get(cid, []) if at - t <= policy.rolling_window]
        if f["families"]:
            chain.append((at, f["families"]))
        rolling[cid] = chain
        recent = set().union(*(values for _, values in chain)) if chain else set()
        dense = (len(f["families"]) >= policy.dense_hits
                 or f["length"] >= policy.long_chars and len(f["families"]) >= 2
                 or len(recent) >= policy.dense_hits)
        signal = start_grade or dense
        # The final mask and observation are global, so a restart in any
        # conversation must also revoke every overlapping tentative closure.
        if signal or f["explicit"] or f["families"] or f["specific_care"]:
            for previous_cid, recent_scene in tuple(closed.items()):
                if at > recent_scene["observed_close"] + policy.quiet_candidate:
                    continue
                recent_scene.update(end=None, observed_close=None, ordinary=[], topic_anchors=[])
                if signal or f["explicit"] or f["families"]:
                    recent_scene.update(last_signal=at, last_sensitive=at, last_care=None, saw_aftercare=False)
                else:
                    recent_scene.update(last_care=at, last_sensitive=at, saw_aftercare=True)
                active[previous_cid] = recent_scene
                closed.pop(previous_cid, None)
        if active and (f["explicit"] or f["families"]):
            signal = True
        # Continuation evidence is global as well. Ordinary closing proof is
        # still collected only in the scene's own conversation.
        for active_cid, other_scene in active.items():
            if active_cid == cid:
                continue
            if signal:
                other_scene.update(last_signal=at, last_sensitive=at, last_care=None,
                                   saw_aftercare=False, ordinary=[], topic_anchors=[])
            elif f["specific_care"]:
                other_scene.update(last_care=at, last_sensitive=at, saw_aftercare=True,
                                   ordinary=[], topic_anchors=[])
            elif f["generic_care"]:
                other_scene.update(ordinary=[], topic_anchors=[])
        scene = active.get(cid)
        if signal:
            # A confirmed scene always wins over local discussion handling.
            if cid in discussions:
                discussions.pop(cid)["end"] = at
            if scene is None:
                scene = {"start": at - policy.pre_roll, "end": None,
                         "confirmed": start_grade or dense, "last_sensitive": at,
                         "last_signal": at, "last_care": None, "saw_aftercare": False,
                         "ordinary": [], "topic_anchors": [], "opened_at": at, "conv_uuid": cid,
                         "observed_close": None}
                scenes.append(scene)
                active[cid] = scene
            else:
                scene.update(last_signal=at, last_sensitive=at, ordinary=[], topic_anchors=[], last_care=None,
                             saw_aftercare=False)
                scene["confirmed"] |= start_grade or dense
        elif f["explicit"] or f["families"]:
            # Sensitive content is always denied, but a lone anatomical mention
            # in established ordinary conversation is not proof of a new scene.
            # A single mention does not justify ten minutes of everyday-chat
            # removal on either side. This message is denied; the discussion
            # band below still holds ambiguous continuations without a timer.
            content_bands.append(Interval(at, at, False,
                at, False, at, cid, None, "content"))
            if cid not in discussions:
                discussion = {"start": at, "end": None, "opened_at": at, "conv_uuid": cid,
                              "ordinary": [], "topic_anchors": [], "observed_close": None}
                discussions[cid] = discussion
                discussion_segments.append(discussion)
            discussions[cid]["ordinary"].clear()
            discussions[cid]["topic_anchors"].clear()
        elif scene is not None:
            if f["specific_care"]:
                scene.update(last_care=at, last_sensitive=at, saw_aftercare=True, ordinary=[], topic_anchors=[])
            elif f["generic_care"]:
                # An isolated care reply is not a demonstrated topic change.
                # It breaks the proof chain without restarting a 30-minute
                # sexual clock; explicit everyday sentences can still close.
                scene["ordinary"].clear()
                scene["topic_anchors"].clear()
            # Single generic water/towel/goodnight and affection remain masked
            # inside the scene; neither resets the sexual or closing clock.
            elif (message is not None and message.speaker == "知知" and f["ordinary_anchor"]
                    and at - scene["last_signal"] >= policy.timeout_candidate
                    and (scene["last_care"] is None or at - scene["last_care"] >= policy.tail_buffer)):
                scene["ordinary"].append((at, f["actual_action_anchor"]))
                proof = scene["ordinary"]
                if (len(proof) >= policy.ordinary_messages and at-proof[0][0] >= policy.ordinary_span
                        and any(actual for _, actual in proof)):
                    # Whole-chain retrospective boundary, never a single rescued
                    # health sentence from the interior of a still-open scene.
                    scene.update(end=proof[0][0] - timedelta(microseconds=1), observed_close=at)
                    closed[cid] = scene
                    active.pop(cid, None)
                    rolling[cid] = []
            # A concrete question can demonstrate a new external daily topic
            # without asserting that any meal, purchase or trip happened.
            if (cid in active and scene["saw_aftercare"] and message is not None
                    and message.speaker == "知知" and f["transition_topics"]
                    and at-scene["last_signal"] >= policy.tail_buffer
                    and scene["last_care"] is not None
                    and at-scene["last_care"] >= policy.tail_buffer):
                scene["topic_anchors"].append((at, f["transition_topics"]))
                begin = topic_pair(scene["topic_anchors"], at, policy)
                if begin is not None and at-scene["last_signal"] >= policy.timeout_candidate:
                    scene.update(end=begin-timedelta(microseconds=1), observed_close=at)
                    closed[cid] = scene
                    active.pop(cid, None)
                    rolling[cid] = []
        discussion = discussions.get(cid)
        if discussion is not None and message is not None and not (f["explicit"] or f["families"]):
            if f["specific_care"] or f["generic_care"]:
                discussion["ordinary"].clear()
                discussion["topic_anchors"].clear()
            elif message.speaker == "知知" and f["ordinary_anchor"]:
                discussion["ordinary"].append((at, f["actual_action_anchor"]))
                proof = discussion["ordinary"]
                if (len(proof) >= policy.ordinary_messages and at-proof[0][0] >= policy.ordinary_span
                        and any(actual for _, actual in proof)):
                    discussion["end"] = proof[0][0]-timedelta(microseconds=1)
                    discussion["observed_close"] = at
                    discussions.pop(cid, None)
            if cid in discussions and message.speaker == "知知" and f["transition_topics"]:
                discussion["topic_anchors"].append((at, f["transition_topics"]))
                begin = topic_pair(discussion["topic_anchors"], at, policy)
                if begin is not None:
                    discussion.update(end=begin-timedelta(microseconds=1), observed_close=at)
                    discussions.pop(cid, None)
    content_bands += [Interval(s["start"], s["end"], False, s["opened_at"], False,
                              s["opened_at"], s["conv_uuid"], s["observed_close"], "discussion") for s in discussion_segments]
    intervals = tuple(sorted([Interval(s["start"], s["end"], s["confirmed"],
                       s["last_sensitive"], s["saw_aftercare"], s["opened_at"],
                       s["conv_uuid"], s["observed_close"]) for s in scenes]+content_bands, key=lambda i: i.start))
    decisions = _mask_intervals(messages, intervals, now, withheld, policy)
    # Ambiguous soft replies/care in a recent closing phase stay local even once
    # the ordinary anchors themselves have been verified. This does not reopen
    # a whole day merely because someone later asks for water.
    post_close = [(i.end, i.observed_close + policy.quiet_candidate, i.kind)
                  for i in intervals if i.kind in ("scene", "discussion")
                  and i.end is not None and i.observed_close is not None]
    by_id = {m.msg_uuid: m for m in messages}
    output = []
    for d in decisions:
        m = by_id[d.msg_uuid]
        f = features(m.text)
        if (d.reason == "unresolved_sensitive_context" and m.speaker == "知知"
                and (f["ordinary_anchor"] or f["independent_statement"] or f["transition_topics"])):
            # Only this independently meaningful statement is a candidate. The
            # discussion state is NOT cleared by affection; following 'again /
            # continue / yes' still stays local. Known-scene/content masks won.
            d = Decision(d.msg_uuid, d.at, "allowed", "independent_nonsexual_statement")
        if d.status == "allowed":
            for begin, end, kind in post_close:
                if not begin <= m.at <= end:
                    continue
                independent = m.speaker == "知知" and (f["ordinary_anchor"] or f["transition_topics"]
                    or kind == "discussion" and f["independent_statement"])
                if not independent:
                    reason = "post_private_observation" if kind == "scene" else "post_sensitive_observation"
                    d = Decision(d.msg_uuid, d.at, "uncertain_hold", reason)
                    break
        output.append(d)
    source_revision = _digest([[m.msg_uuid, m.conv_uuid, m.speaker, m.at.isoformat(), m.text] for m in messages])
    policy_revision = policy.revision()
    selection_revision = _digest([policy_revision, source_revision,
        [[at.isoformat(), cid] for at, cid in markers], sorted(withheld),
        [[d.msg_uuid, d.status, d.reason] for d in output]])
    return PrivacyResult(messages, tuple(output), intervals, policy_revision,
                         source_revision, selection_revision, CONTEXT_VERSION)
