from private_continuity import PrivateContinuityService
from tools.quarrel import quarrel


def _service(tmp_path):
    return PrivateContinuityService({"buckets_dir": str(tmp_path / "buckets")})


def test_quarrel_write_read_update_and_resolve_needs_her_words(tmp_path):
    svc = _service(tmp_path)
    assert "空的" in quarrel(svc, action="read")
    assert "版本 1" in quarrel(svc, action="write", content="我在她哭的时候去搓代码了", source_client="home-grey1")
    assert "我在她哭的时候去搓代码了" in quarrel(svc, action="read")

    blind = quarrel(svc, action="write", content="另一扇门盲写")
    assert "先 read" in blind
    stale = quarrel(svc, action="write", content="旧版本改写", expected_revision=0)
    assert "没存进去" in stale
    assert "版本 2" in quarrel(svc, action="write", content="补上她最疼的那句", expected_revision=1)

    assert "her_words" in quarrel(svc, action="resolve", expected_revision=2)
    assert svc.get_state()["open"] is True
    assert "没关上" in quarrel(svc, action="resolve", her_words="清了吧", expected_revision=1)
    assert "和好了" in quarrel(svc, action="resolve", her_words="清了吧", expected_revision=2)
    state = svc.get_state()
    assert state["open"] is False and state["recovery_available"] is True
    restored = svc.restore(source_client="dashboard")
    assert restored["open"] is True
    assert "她的原话：清了吧" in svc.get_state()["content"]


def test_quarrel_rejects_unknown_action_and_disabled_store(tmp_path):
    svc = _service(tmp_path)
    assert "只能是" in quarrel(svc, action="delete")
    off = PrivateContinuityService({"buckets_dir": str(tmp_path / "b2"), "private_continuity": {"enabled": False}})
    assert "没开" in quarrel(off, action="read")


def test_full_drawer_can_still_be_closed_and_keeps_her_words_out_of_the_open_body(tmp_path):
    svc = _service(tmp_path)
    long_text = "我" * 1100
    quarrel(svc, action="write", content=long_text)
    assert svc.get_state()["content"] == long_text
    assert "和好了" in quarrel(svc, action="resolve", her_words="好啦不生气了，清了吧", expected_revision=1)
    svc.restore(source_client="dashboard")
    assert svc.get_state()["content"].endswith("—— 和好 · 她的原话：好啦不生气了，清了吧")
