"""The Help page (app/static/help.html) and the links into it.

Help links from Settings once pointed at a page that did not exist yet; the
checks below make sure every link the app uses lands on a real section."""

import re

from fastapi.testclient import TestClient

from app import api

STATIC = api.STATIC_DIR
HELP = (STATIC / "help.html").read_text(encoding="utf-8")
IDS = set(re.findall(r'\bid="([^"]+)"', HELP))


def test_help_is_served(tmp_path):
    app = api.create_app(db_path=tmp_path / "app.db", reference_dir=tmp_path / "none")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/static/help.html")
    assert response.status_code == 200
    assert "Setting up Google Takeout" in response.text


def test_every_help_link_in_settings_has_a_section():
    setup_js = (STATIC / "setup.js").read_text(encoding="utf-8")
    anchors = set(re.findall(r'helpLink\("([^"]+)"', setup_js))
    assert anchors, "setup.js should link into the help"
    assert anchors <= IDS, f"missing help sections: {sorted(anchors - IDS)}"


def test_every_link_inside_the_help_resolves():
    targets = set(re.findall(r'href="#([^"]+)"', HELP))
    assert targets <= IDS, f"broken in-page links: {sorted(targets - IDS)}"


def test_the_sections_the_prompt_asks_for_exist():
    for section in ("getting-started", "views", "map", "threads", "panels",
                    "your-data", "takeout", "advanced", "troubleshooting",
                    "no-profiles", "no-zips", "sync-errors", "logs",
                    "storage", "reset", "uninstall"):
        assert section in IDS, section


def test_unverified_details_are_visibly_marked():
    """Every TODO(Joe) must render on the page, not hide in a comment.

    HTML comments are left out of the count: the page's header comment
    mentions the marker by name to explain it."""
    body = re.sub(r"<!--.*?-->", "", HELP, flags=re.S)
    visible = len(re.findall(r'<span class="todo">TODO\(Joe\)', body))
    assert visible == body.count("TODO(Joe)")


def test_every_help_image_exists():
    """Screenshots are referenced relative to help.html and ship with it
    (app/static is part of the installed app), so a typo or a file left
    out of git would be a broken image in every install."""
    sources = re.findall(r'<img[^>]*\bsrc="([^"]+)"', HELP)
    missing = [src for src in sources if not (STATIC / src).is_file()]
    assert not missing, f"missing help images: {missing}"


def test_help_is_plain_ascii():
    HELP.encode("ascii")


def test_the_window_has_a_help_button_and_viewer():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-help"' in index and 'id="help-frame"' in index
    assert '/static/help.js' in index
    assert (STATIC / "help.js").exists()
