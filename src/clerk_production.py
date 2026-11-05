"""Reviewed production owner for the otherwise locked scene-mask producer.

Only an explicit matching profile approval may dispatch this input. The day
has a fixed end plus one hour of local boundary context; later days cannot
churn its identity. Unassembled or withdrawn visible history fails closed.
"""
from dataclasses import replace
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

from clerk_privacy import PrivacyInputError,_aware,_digest
from clerk_redaction import ClerkLexicon
from clerk_scene_detector import SceneDetectionPolicy,prepare_automatic_scene_day

PROFILE = 'scene_word_mask'
VERSION = 'production-scene-mask-fixed-prefix-v1'
LOOKAHEAD = timedelta(hours=1)


def approval_revision(lexicon, *, timezone_name='Asia/Shanghai', cutoff_hour=4):
    return _digest([VERSION,SceneDetectionPolicy().revision(),lexicon.revision(),
                    timezone_name,cutoff_hour,LOOKAHEAD.total_seconds(),'complete-visible-prefix-no-gaps'])


def prepare_production_day(snapshot,memory_day,*,lexicon_path,approved_revision,now,
                           timezone_name='Asia/Shanghai',cutoff_hour=4):
    if snapshot.get('complete') is not True:
        raise PrivacyInputError('complete assembled visible snapshot required')
    lexicon=ClerkLexicon.load(lexicon_path)
    expected=approval_revision(lexicon,timezone_name=timezone_name,cutoff_hour=cutoff_hour)
    if approved_revision != expected:
        raise PrivacyInputError('scene input profile approval required')
    tz=ZoneInfo(timezone_name)
    start=datetime.combine(memory_day,datetime.min.time(),tzinfo=tz)+timedelta(hours=cutoff_hour)
    end=start+timedelta(days=1)+LOOKAHEAD
    if _aware(now)<_aware(end):
        raise PrivacyInputError('scene boundary context not yet complete')
    # The archive supplies spoken text only. No native field is copied through.
    rows=[]
    for row in snapshot['rows']:
        if _aware(row['at'])<_aware(end):
            rows.append({k:row[k] for k in ('msg_uuid','conv_uuid','speaker','at','text')})
    if any(_aware(g['at'])<_aware(end) for g in snapshot.get('gaps',())):
        raise PrivacyInputError('visible history gap requires review')
    beginning=min([_aware(start),*(_aware(r['at']) for r in rows)])-SceneDetectionPolicy().pre_roll
    day,receipt=prepare_automatic_scene_day(rows,memory_day,window_start=beginning,window_end=end,
        lexicon=lexicon,snapshot_complete=True,timezone_name=timezone_name,cutoff_hour=cutoff_hour)
    if (day.privacy_review_required is not True or receipt['scene_basis']!='automatic_candidate'
            or receipt['detector_revision']!=SceneDetectionPolicy().revision()
            or day.redaction_policy!=lexicon.revision()):
        raise PrivacyInputError('unexpected scene producer contract')
    # This reviewed owner checks assembly, profile, boundaries and producer
    # identity. Public preview helpers remain locked for all other callers.
    return replace(day,privacy_review_required=False,
        selection_revision=_digest([day.selection_revision,VERSION,expected]))
