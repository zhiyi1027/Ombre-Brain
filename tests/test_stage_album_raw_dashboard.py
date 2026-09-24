from pathlib import Path


def test_dashboard_has_album_and_transcript_tabs():
    html = (Path(__file__).resolve().parents[1] / "frontend" / "dashboard.html").read_text(encoding="utf-8")
    for marker in [
        'data-tab="album"', 'data-tab="raw"', 'id="album-view"', 'id="raw-view"',
        "if (target === 'album') loadAlbum();", "if (target === 'raw') loadRawDays();",
        "BASE + '/api/album'", "BASE + '/api/raw/days'", "'/api/raw/search?q='",
    ]:
        assert marker in html
