import re
from datetime import datetime, timezone

from bot.feeds import default_context
from bot.journal import Journal
from bot.muse import TOOL_REGISTRY, dispatch_tool, route_http
from bot.persist import load_bundle
from bot.research import route_chat
from bot.schedule import run_scheduled_session
from bot.shapes import Bar, ChainSnapshot, OptionContract, default_operator_config
from bot.store import SnapshotStore
from bot.video import build_video_brief, composition_html, render_video, write_video_project

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()


def _contract(strike, side, oi, gamma):
    return OptionContract(
        strike=strike, expiry="2026-11-20", call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30,
    )


def _seeded_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 3000.0, 0.05),
                _contract(105.0, "call", 3000.0, 0.05),
            ),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    return store


def _many_contract_store() -> SnapshotStore:
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="TEST",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=tuple(_contract(90.0 + strike, "call", 1000.0, 0.05) for strike in range(8)),
        )
    )
    store.save_bar("TEST", Bar(interval="1d", timestamp_utc=NOW,
                               open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    return store


def _root_attrs(page: str) -> str:
    match = re.search(r"<div ([^>]*data-composition-id[^>]*)>", page)
    assert match is not None
    return match.group(1)


def test_composition_root_carries_hyperframes_contract():
    brief = build_video_brief(["TEST"], _seeded_store(), Journal(), NOW)
    page = composition_html(brief)
    assert page.startswith("<!doctype html>")
    assert 'name="viewport" content="width=1920, height=1080"' in page
    attrs = _root_attrs(page)
    assert 'data-composition-id="video-brief-' in attrs
    assert 'data-start="0"' in attrs
    assert 'data-width="1920"' in attrs
    assert 'data-height="1080"' in attrs
    assert f'data-duration="{brief.total_seconds}"' in attrs
    assert "data-no-timeline" in attrs
    assert "<script" not in page
    assert "z-index" not in page


def test_scene_clips_chain_with_no_gaps_and_match_root_total():
    brief = build_video_brief(["TEST"], _seeded_store(), Journal(), NOW)
    page = composition_html(brief)
    clips = re.findall(
        r'<section id="[^"]*" class="clip" data-track-index="\d+" '
        r'data-start="(\d+)" data-duration="(\d+)">', page
    )
    assert len(clips) == len(brief.scenes) == 5
    assert [int(duration) for _, duration in clips] == [4, 6, 5, 6, 4]
    cursor = 0
    for start, duration in clips:
        assert int(start) == cursor
        cursor += int(duration)
    assert cursor == brief.total_seconds
    assert f'data-duration="{cursor}"' in _root_attrs(page)


def test_gamma_scene_embeds_flip_value_and_wall_strikes():
    page = composition_html(build_video_brief(["TEST"], _seeded_store(), Journal(), NOW))
    match = re.search(r'<section id="gamma-TEST" class="clip"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None
    body = match.group(1)
    assert "flip 105.0" in body
    assert "95" in body
    assert "105" in body
    assert "<svg" in body


def test_unknown_symbol_yields_plain_no_snapshot_scene():
    brief = build_video_brief(["NOPE"], SnapshotStore(), Journal(), NOW)
    assert len(brief.scenes) == 5
    page = composition_html(brief)
    assert "no snapshot stored for NOPE" in page


def test_hostile_underlying_text_is_escaped():
    hostile = '<script>alert("x")</script>'
    page = composition_html(build_video_brief([hostile], SnapshotStore(), Journal(), NOW))
    assert hostile not in page
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_build_caps_symbols_at_four_and_ideas_at_five():
    brief = build_video_brief(["A", "B", "C", "D", "E"], SnapshotStore(), Journal(), NOW)
    assert brief.underlyings == ("A", "B", "C", "D")
    assert len([scene for scene in brief.scenes if scene.scene_id.startswith("gamma-")]) == 4
    assert brief.total_seconds == 4 + 4 * 6 + 5 + 6 + 4
    page = composition_html(build_video_brief(["TEST"], _many_contract_store(), Journal(), NOW))
    match = re.search(r'<section id="ideas" class="clip"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None
    assert match.group(1).count("<li>") == 5


def test_outro_carries_paper_pnl_first_line():
    page = composition_html(build_video_brief(["TEST"], _seeded_store(), Journal(), NOW))
    assert "paper pnl: no fills journaled" in page


def test_write_video_project_writes_parseable_index(tmp_path):
    brief = build_video_brief(["TEST"], _seeded_store(), Journal(), NOW)
    path = write_video_project(str(tmp_path / "video-brief"), brief)
    assert path.endswith("index.html")
    text = (tmp_path / "video-brief" / "index.html").read_text()
    assert text.startswith("<!doctype html>")
    assert "data-composition-id" in text
    assert "data-no-timeline" in text


def test_render_video_without_npx_returns_not_ok(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = render_video("proj", "brief.mp4")
    assert result.ok is False
    assert result.output == "brief.mp4"
    assert "npx" in result.error


def test_route_chat_video_brief_journals_and_names_project_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store, journal = _seeded_store(), Journal()
    reply = route_chat("video brief", "TEST", store, journal, CONFIG, NOW)
    assert "data/video-brief" in reply
    assert "npx hyperframes render --output brief.mp4" in reply
    assert (tmp_path / "data" / "video-brief" / "index.html").exists()
    entries = [entry for entry in journal.entries() if entry.kind == "video_brief"]
    assert len(entries) == 1
    assert entries[0].payload["project_dir"] == "data/video-brief"
    assert entries[0].payload["underlyings"] == ["TEST"]


def test_tool_registry_exposes_get_video_brief(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert TOOL_REGISTRY["get_video_brief"].description == (
        "Render-ready HyperFrames video brief project from stored snapshots"
    )
    reply = dispatch_tool(
        "get_video_brief", {"query": "video brief"}, _seeded_store(), Journal(), CONFIG, NOW,
        underlying="TEST",
    )
    assert reply.ok is True
    assert "data/video-brief" in reply.reply


def test_route_http_get_video_serves_html_and_400s_unknown():
    response = route_http("GET", "/video?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "data-composition-id" in response.body.decode("utf-8")
    missing = route_http("GET", "/video?underlying=NOPE", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert missing.status == 400
    journal = Journal()
    route_http("GET", "/video?underlying=TEST", {}, _seeded_store(), journal, CONFIG, NOW)
    assert journal.entries() == []


def test_run_scheduled_session_writes_video_project(tmp_path):
    reply = run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    assert "total exposure" in reply
    page = (tmp_path / "video-brief" / "index.html").read_text()
    assert "data-composition-id" in page
    assert "data-no-timeline" in page


def test_full_frame_layout_sizes_root_and_absolutes_clips():
    page = composition_html(build_video_brief(["TEST"], _seeded_store(), Journal(), NOW))
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    root = re.search(r"div\[data-composition-id\]\{([^}]*)\}", style).group(1)
    assert "position:relative" in root
    assert "width:1920px" in root
    assert "height:1080px" in root
    assert "overflow:hidden" in root
    assert "padding" not in root
    assert "margin" not in root
    clip = re.search(r"\.clip\{([^}]*)\}", style).group(1)
    assert "position:absolute" in clip
    assert "inset:0" in clip


def test_every_clip_carries_its_own_track_lane():
    page = composition_html(build_video_brief(["TEST"], _seeded_store(), Journal(), NOW))
    lanes = re.findall(r'<section [^>]*data-track-index="(\d+)"', page)
    assert lanes == ["1", "2", "3", "4", "5"]


def test_storyboard_variant_overrides_layout_but_keeps_timeline():
    brief = build_video_brief(["TEST"], _seeded_store(), Journal(), NOW)
    render = composition_html(brief)
    board = composition_html(brief, storyboard=True)
    assert "storyboard-override" not in render
    assert "storyboard-override" in board
    override = board.split("storyboard-override")[1]
    assert "position:static" in override
    assert "height:auto" in override
    assert "max-width:1920px" in override
    pairs = r'data-start="(\d+)" data-duration="(\d+)"'
    assert re.findall(pairs, board) == re.findall(pairs, render)
    assert _root_attrs(board) == _root_attrs(render)


def test_write_video_project_writes_render_composition(tmp_path):
    brief = build_video_brief(["TEST"], _seeded_store(), Journal(), NOW)
    path = write_video_project(str(tmp_path / "video-brief"), brief)
    text = open(path, encoding="utf-8").read()
    assert "storyboard-override" not in text
    assert ".clip{position:absolute" in text


def test_compact_money_for_billion_scale_walls():
    store = SnapshotStore()
    store.save(
        ChainSnapshot(
            underlying="BIG",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(
                _contract(95.0, "put", 3000000.0, 0.05),
                _contract(105.0, "call", 3000000.0, 0.05),
            ),
        )
    )
    store.save_bar("BIG", Bar(interval="1d", timestamp_utc=NOW,
                              open=99.0, high=101.0, low=98.0, close=100.0, volume=1000.0))
    page = composition_html(build_video_brief(["BIG"], store, Journal(), NOW))
    match = re.search(r'<section id="gamma-BIG" class="clip"[^>]*>(.*?)</section>', page, re.S)
    assert match is not None
    assert "$1.5B" in match.group(1)
    assert "-$1.5B" in match.group(1)


def test_run_scheduled_session_persists_video_brief_journal(tmp_path):
    reply = run_scheduled_session("TEST", str(tmp_path), NOW, context_factory=default_context)
    assert "total exposure" in reply
    assert (tmp_path / "video-brief" / "index.html").exists()
    _, journal = load_bundle(str(tmp_path))
    kinds = [entry.kind for entry in journal.entries()]
    assert "video_brief" in kinds


def test_route_http_get_video_storyboard_returns_override():
    response = route_http(
        "GET", "/video?underlying=TEST&storyboard=1", {}, _seeded_store(), Journal(), CONFIG, NOW
    )
    assert response.status == 200
    assert "storyboard-override" in response.body.decode("utf-8")
    plain = route_http("GET", "/video?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert plain.status == 200
    assert "storyboard-override" not in plain.body.decode("utf-8")
