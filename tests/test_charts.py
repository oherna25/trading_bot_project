import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bot.charts import (
    CHART_MAX_STRIKES,
    CHART_VIEWS,
    board_rows,
    chart_strikes,
    chart_view,
    gamma_chart_html,
    gamma_svg,
    ladder_strikes,
    regime_board_html,
)
from bot.diff import compact_dollars
from bot.exposure import compute_exposures, compute_gex
from bot.journal import Journal
from bot.muse import route_http
from bot.shapes import Bar, ChainSnapshot, DashboardSettings, GexProfile, OptionContract, default_operator_config
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
CONFIG = default_operator_config()
VIEW_KEYS = ("gex", "heatmap", "vex", "dex", "charm", "vanna")
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _contract(strike, side, oi, gamma, expiry="2026-11-20", **greeks):
    return OptionContract(
        strike=strike, expiry=expiry, call_or_put=side, open_interest=oi,
        gamma=gamma, price=2.50, bid=2.40, ask=2.60, iv=0.30, **greeks,
    )


def _store_with(symbol, contracts, spot=100.0) -> SnapshotStore:
    store = SnapshotStore()
    store.save(ChainSnapshot(
        underlying=symbol, timestamp_utc=NOW, source_name="tradier_sandbox",
        latency_ms=9.0, contracts_list=tuple(contracts),
    ))
    if spot is not None:
        store.save_bar(symbol, Bar(interval="1d", timestamp_utc=NOW, open=spot - 1.0, high=spot + 1.0,
                                   low=spot - 2.0, close=spot, volume=1000.0))
    return store


def _seeded_store() -> SnapshotStore:
    store = _store_with("TEST", (_contract(95.0, "put", 3000.0, 0.05), _contract(105.0, "call", 3000.0, 0.05)))
    store.save(
        ChainSnapshot(
            underlying="DEMO",
            timestamp_utc=NOW,
            source_name="tradier_sandbox",
            latency_ms=9.0,
            contracts_list=(_contract(50.0, "call", 1000.0, 0.05),),
        )
    )
    store.save_bar("DEMO", Bar(interval="1d", timestamp_utc=NOW,
                               open=49.0, high=51.0, low=48.0, close=50.0, volume=500.0))
    return store


def _full_greeks_store() -> SnapshotStore:
    """Three expiries, nine strikes around 100, every greek populated, calls dominate above spot."""
    contracts = []
    for expiry_index, expiry in enumerate(("2026-11-20", "2026-11-27", "2026-12-18")):
        for strike in range(80, 125, 5):
            for side in ("call", "put"):
                oi = 500.0 + 40.0 * (strike - 80) if side == "call" else 2300.0 - 40.0 * (strike - 80)
                contracts.append(_contract(
                    float(strike), side, oi + 100.0 * expiry_index, 0.02 + 0.001 * expiry_index,
                    expiry=expiry, vega=0.1 * (1 if strike >= 100 else 2), delta=0.4 if side == "call" else -0.4,
                    charm=0.01, vanna=0.02,
                ))
    return _store_with("FULL", contracts)


def _profile(store, symbol="TEST"):
    snapshot_id, snapshot = store.read_latest(symbol)
    bar = store.read_latest_bar(symbol)
    return snapshot_id, snapshot, compute_gex(snapshot, snapshot_id, spot=bar.close)


def _chart(store, symbol, view=None, settings=None):
    path = f"/chart?underlying={symbol}" + (f"&view={view}" if view is not None else "")
    if settings is not None:
        store.save_settings(settings)
    return route_http("GET", path, {}, store, Journal(), CONFIG, NOW)


def _without_style(body: str) -> str:
    return re.sub(r"<style>.*?</style>", "", body, flags=re.S)


def _ladder_labels(svg: str) -> list[float]:
    pattern = r'<text class="(?:strong|dim)" x="[\d.]+" y="[\d.]+" text-anchor="end">([\d.]+)</text>'
    return [float(found) for found in re.findall(pattern, svg)]


def _marker_y(svg: str, kind: str) -> float:
    return float(re.search(rf'<line class="{kind}" x1="\d+" y1="([\d.]+)"', svg).group(1))


def _tabs(body: str) -> str:
    return re.search(r'<nav class="tabs".*?</nav>', body, re.S).group(0)


def test_compute_gex_splits_call_and_put_per_strike():
    _, _, profile = _profile(_seeded_store())
    assert profile.per_strike_call_exposure[105.0] > 0
    assert profile.per_strike_put_exposure[95.0] < 0
    assert profile.per_strike_call_exposure.get(95.0, 0.0) == 0.0
    assert profile.per_strike_put_exposure.get(105.0, 0.0) == 0.0
    assert profile.flip_point == 105.0


def test_chart_strikes_sorted_and_capped():
    _, _, profile = _profile(_seeded_store())
    assert chart_strikes(profile) == [95.0, 105.0]
    assert len(chart_strikes(profile, max_strikes=1)) == 1


def test_ladder_takes_the_contiguous_window_nearest_spot():
    magnitudes = {float(strike): float(strike % 7) for strike in range(100, 200)}
    window = ladder_strikes(magnitudes, spot=150.2)
    assert len(window) == CHART_MAX_STRIKES
    assert window == sorted(window)
    assert window == [float(strike) for strike in range(int(window[0]), int(window[0]) + CHART_MAX_STRIKES)]
    assert window[0] <= 150.0 <= window[-1]


def test_ladder_without_spot_keeps_the_largest_by_magnitude():
    magnitudes = {float(strike): 1.0 for strike in range(100, 140)}
    magnitudes[100.0] = 500.0
    magnitudes[139.0] = -900.0
    picked = ladder_strikes(magnitudes, spot=None)
    assert len(picked) == CHART_MAX_STRIKES
    assert 100.0 in picked and 139.0 in picked


def test_gamma_svg_is_a_strike_ladder_with_the_highest_strike_on_top():
    store = _full_greeks_store()
    _, _, profile = _profile(store, "FULL")
    svg = gamma_svg(profile)
    labels = _ladder_labels(svg)
    assert labels == sorted(chart_strikes(profile), reverse=True)
    assert labels[0] == 120.0 and labels[-1] == 80.0


def test_gamma_svg_scales_with_its_container_and_grows_with_rows():
    _, _, small = _profile(_seeded_store())
    _, _, large = _profile(_full_greeks_store(), "FULL")

    def view_box_height(svg):
        return float(re.search(r'viewBox="0 0 \d+ ([\d.]+)"', svg).group(1))

    for svg in (gamma_svg(small), gamma_svg(large)):
        assert svg.startswith("<svg")
        assert 'width="100%"' in svg
    assert view_box_height(gamma_svg(large)) > view_box_height(gamma_svg(small))


def test_gamma_svg_marks_calls_puts_flip_spot_and_walls():
    _, _, profile = _profile(_seeded_store())
    svg = gamma_svg(profile)
    assert f"<title>105 calls {compact_dollars(1_500_000.0)}</title>" in svg
    assert f"<title>95 puts {compact_dollars(-1_500_000.0)}</title>" in svg
    assert "flip 105.0" in svg
    assert "spot 100.00" in svg
    assert svg.count('class="pos wall"') == 1
    assert svg.count('class="neg wall"') == 1


def test_gamma_svg_emphasizes_only_the_largest_wall_on_each_side():
    profile = GexProfile(
        snapshot_id="TEST:snap-1",
        per_strike_exposure={100.0: 3.0, 105.0: 9.0, 110.0: -4.0, 115.0: -8.0},
        total_exposure=0.0,
        flip_point=None,
        spot=107.0,
        per_strike_call_exposure={100.0: 3.0, 105.0: 9.0},
        per_strike_put_exposure={110.0: -4.0, 115.0: -8.0},
    )
    svg = gamma_svg(profile)
    wall_titles = re.findall(r'class="(?:pos|neg) wall"[^>]*><title>([^<]+)</title>', svg)
    assert sorted(wall_titles) == ["105 calls $9", "115 puts -$8"]


def test_gamma_svg_labels_each_side_peak():
    profile = GexProfile(
        snapshot_id="TEST:snap-1",
        per_strike_exposure={100.0: 1_000_000.0, 105.0: -100_000.0},
        total_exposure=900_000.0,
        flip_point=None,
        spot=None,
        per_strike_call_exposure={100.0: 1_000_000.0},
        per_strike_put_exposure={105.0: -100_000.0},
    )
    svg = gamma_svg(profile)
    assert ">$1M</text>" in svg
    assert ">-$100K</text>" in svg
    assert 'class="spot"' not in svg and 'class="flip"' not in svg


def test_spot_marker_sits_between_the_rows_it_falls_between():
    def profile_at(spot):
        return GexProfile(
            snapshot_id="TEST:snap-1",
            per_strike_exposure={95.0: -1.0, 105.0: 1.0},
            total_exposure=0.0, flip_point=None, spot=spot,
            per_strike_call_exposure={105.0: 1.0}, per_strike_put_exposure={95.0: -1.0},
        )

    near_low, middle, near_high = (_marker_y(gamma_svg(profile_at(spot)), "spot") for spot in (96.0, 100.0, 104.0))
    assert near_high < middle < near_low
    assert middle - near_high == pytest.approx(near_low - middle)


def test_markers_outside_the_ladder_are_flagged_with_an_arrow():
    profile = GexProfile(
        snapshot_id="TEST:snap-1",
        per_strike_exposure={95.0: -1.0, 105.0: 1.0},
        total_exposure=0.0, flip_point=140.0, spot=60.0,
        per_strike_call_exposure={105.0: 1.0}, per_strike_put_exposure={95.0: -1.0},
    )
    svg = gamma_svg(profile)
    assert "\u2193 spot 60.00" in svg
    assert "\u2191 flip 140.0" in svg


def test_empty_profile_renders_a_placeholder_instead_of_failing():
    svg = gamma_svg(GexProfile(snapshot_id="X", per_strike_exposure={}, total_exposure=0.0, flip_point=None))
    assert svg.startswith("<svg") and "no strikes" in svg


def test_chart_view_registry_has_the_documented_tabs_and_falls_back_to_gex():
    assert tuple(view.key for view in CHART_VIEWS) == VIEW_KEYS
    assert len({view.label for view in CHART_VIEWS}) == len(CHART_VIEWS)
    assert chart_view("vanna").key == "vanna"
    assert chart_view("nope").key == "gex"
    assert chart_view("").key == "gex"


def test_gamma_chart_html_refreshes_and_labels():
    store = _seeded_store()
    snapshot_id, snapshot = store.read_latest("TEST")
    bar = store.read_latest_bar("TEST")
    page = gamma_chart_html("TEST", snapshot, _profile(store)[2], bar, NOW)
    assert page.startswith("<!doctype html>")
    assert 'http-equiv="refresh" content="60"' in page
    assert "TEST gamma by strike" in page
    assert "flip 105.0" in page
    assert snapshot_id in page


def test_stat_tiles_sit_above_the_chart_with_spot_flip_walls_and_regime():
    body = _chart(_seeded_store(), "TEST").body.decode("utf-8")
    stats = re.search(r'<div class="stats">.*?</div></div>', body, re.S).group(0)
    tiles = dict(
        (label, value)
        for value, label in re.findall(r'<div class="stat"><b[^>]*>([^<]*)</b><span>([^<]*)</span>', stats)
    )
    assert tiles["spot"] == "100.00"
    assert tiles["zero gamma"] == "105.0 (+5.0%)"
    assert tiles["call wall"] == f"105 {compact_dollars(1_500_000.0)}"
    assert tiles["put wall"] == f"95 {compact_dollars(-1_500_000.0)}"
    assert tiles["regime"] == "long-gamma"
    assert "net GEX" in tiles
    assert body.index('class="stats"') < body.index('<nav class="tabs"') < body.index('class="chart-panel"')


def test_stat_tiles_say_n_a_when_data_is_missing():
    store = _store_with("BARE", (_contract(50.0, "call", 1000.0, 0.05),), spot=None)
    response = _chart(store, "BARE")
    assert response.status == 200
    body = response.body.decode("utf-8")
    tiles = dict(
        (label, value)
        for value, label in re.findall(r'<div class="stat"><b[^>]*>([^<]*)</b><span>([^<]*)</span>', body)
    )
    assert tiles["spot"] == "n/a"
    assert tiles["zero gamma"] == "n/a"
    assert tiles["put wall"] == "n/a"
    assert tiles["call wall"].startswith("50 ")


def test_every_view_renders_without_hard_coded_colors_on_a_full_chain():
    store = _full_greeks_store()
    for key in VIEW_KEYS:
        response = _chart(store, "FULL", key)
        assert response.status == 200, key
        body = _without_style(response.body.decode("utf-8"))
        chart = body[body.index('<nav class="tabs"'):]
        assert "<svg" in chart or '<table class="heat"' in chart, key
        assert not HEX_COLOR.findall(chart), key
        assert 'fill="#' not in chart and 'stroke="#' not in chart, key


def test_every_view_survives_a_gamma_only_single_strike_chain_with_no_spot():
    store = _store_with("ONE", (_contract(50.0, "call", 1000.0, 0.05),), spot=None)
    for key in VIEW_KEYS:
        response = _chart(store, "ONE", key)
        assert response.status == 200, key
        assert "<h1>" in response.body.decode("utf-8")


def test_views_without_greeks_say_so_instead_of_drawing_empty_axes():
    store = _seeded_store()
    for key, word in (("vex", "vega"), ("dex", "delta"), ("charm", "charm"), ("vanna", "vanna")):
        body = _chart(store, "TEST", key).body.decode("utf-8")
        assert f"No {word} exposure in this snapshot" in body, key
        assert "<svg class=\"cx ladder\"" not in body


def test_greek_views_draw_a_net_ladder_per_strike():
    store = _full_greeks_store()
    snapshot_id, snapshot = store.read_latest("FULL")
    _, vex, *_ = compute_exposures(snapshot, snapshot_id, spot=100.0)
    vex_body = _chart(store, "FULL", "vex").body.decode("utf-8")
    assert "vega exposure by strike" in vex_body
    assert f"<title>120 net {compact_dollars(vex.per_strike_exposure[120.0])}</title>" in vex_body
    dex_body = _chart(store, "FULL", "dex").body.decode("utf-8")
    assert "$" not in re.search(r'<svg class="cx ladder".*?</svg>', dex_body, re.S).group(0)


def test_tabs_mark_only_the_active_view_and_keep_the_symbol():
    store = _seeded_store()
    for key in VIEW_KEYS:
        tabs = _tabs(_chart(store, "TEST", key).body.decode("utf-8"))
        assert tabs.count("aria-current") == 1
        active = re.search(r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', tabs)
        assert f"view={key}" in active.group(1)
        assert "underlying=TEST" in active.group(1)
        assert active.group(2) == chart_view(key).label
        for other in VIEW_KEYS:
            assert f"view={other}" in tabs


def test_unknown_view_serves_the_gex_ladder():
    store = _seeded_store()
    body = _chart(store, "TEST", "bogus").body.decode("utf-8")
    assert "TEST gamma by strike" in body
    assert re.search(r'view=gex" aria-current="page">GEX</a>', _tabs(body))
    assert _chart(store, "TEST", "bogus").body == _chart(store, "TEST", "gex").body


def test_view_survives_the_ticker_search_and_the_theme_toggle():
    body = _chart(_seeded_store(), "TEST", "heatmap").body.decode("utf-8")
    assert '<input type="hidden" name="view" value="heatmap">' in body
    assert 'name="next" value="/chart?underlying=TEST&amp;view=heatmap"' in body


def test_heatmap_rows_are_strikes_columns_are_expiries_and_totals_match_the_gex_view():
    store = _full_greeks_store()
    snapshot_id, snapshot, profile = _profile(store, "FULL")
    body = _chart(store, "FULL", "heatmap").body.decode("utf-8")
    table = re.search(r'<table class="heat">.*?</table>', body, re.S).group(0)
    assert "gamma by strike and expiry" in body
    for expiry in ("2026-11-20", "2026-11-27", "2026-12-18"):
        assert f"<th>{expiry}</th>" in table
    rows = re.findall(r'<tr(?: class="near")?><td class="k">([\d.]+)(?:<span class="tag">spot</span>)?</td>(.*?)</tr>', table, re.S)
    strikes = [float(strike) for strike, _ in rows]
    assert strikes == sorted(strikes, reverse=True)
    for strike, cells in rows:
        total = re.findall(r'<td class="cell [^"]*total"[^>]*>([^<]*)</td>', cells)[-1]
        assert total == compact_dollars(profile.per_strike_exposure[float(strike)])
    assert table.count('<tr class="near">') == 1
    assert '<tr class="near"><td class="k">100<span class="tag">spot</span>' in table


def test_heatmap_tints_by_share_of_the_largest_cell_using_theme_variables():
    body = _chart(_full_greeks_store(), "FULL", "heatmap").body.decode("utf-8")
    intensities = [float(found) for found in re.findall(r'class="cell (?:pos|neg)"[^>]*style="--i:([\d.]+)"', body)]
    assert intensities and max(intensities) == 1.0 and min(intensities) >= 0.0
    assert 'class="cell pos"' in body and 'class="cell neg"' in body


def test_heatmap_caps_columns_at_eight_expiries_and_folds_the_rest_into_later():
    days = [(datetime(2026, 11, 20) + timedelta(days=7 * n)).date().isoformat() for n in range(10)]
    contracts = [
        _contract(strike, side, 500.0, 0.03, expiry=expiry)
        for expiry in days for strike in (95.0, 100.0, 105.0) for side in ("call", "put")
    ]
    store = _store_with("WIDE", contracts)
    _, snapshot, profile = _profile(store, "WIDE")
    table = re.search(r'<table class="heat">.*?</table>', _chart(store, "WIDE", "heatmap").body.decode(), re.S).group(0)
    headers = re.findall(r"<th[^>]*>([^<]*)</th>", table)
    assert headers[1:9] == days[:8]
    assert "+2 later" in headers
    assert days[8] not in table and days[9] not in table
    assert profile.per_strike_exposure


def test_dashboard_shows_the_gex_profile_above_the_digest_and_links_to_all_views():
    body = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW).body.decode()
    assert body.index("GEX profile") < body.index("<h2>Digest</h2>")
    assert 'class="cx ladder"' in body
    assert 'href="/chart?underlying=TEST">All chart views</a>' in body
    skipped = route_http("GET", "/dashboard?underlying=NOPE", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert skipped.status == 200
    assert "GEX profile" not in skipped.body.decode()


def test_trade_card_and_track_record_charts_use_theme_classes_not_hex():
    from bot.trackrecord import equity_svg
    from bot.tradecards import TradeCard, payoff_svg, spot_flip_gauge_svg

    single = TradeCard(
        symbol="TEST", direction="short", spread="single put 100.0", strike=100.0,
        expiry="2026-11-20", call_or_put="put", hedge_strike=None, confidence=0.65,
        snapshot_id="TEST:x", max_loss=250.0, breakeven=97.5, net_debit=2.5, spread_pct=8.0,
        wall_exposure=-1_500_000.0, regime="short-gamma", spot=102.0, flip_point=None,
        reason="r", reason_source="fallback",
    )
    vertical = replace(single, spread="100/105 call vertical", call_or_put="call", hedge_strike=105.0)
    svgs = [payoff_svg(single), payoff_svg(vertical)] + [
        spot_flip_gauge_svg(100.0, 105.0),
        spot_flip_gauge_svg(None, 105.0),
        equity_svg([(NOW, 10.0), (NOW + timedelta(days=1), -5.0)]),
        equity_svg([]),
    ]
    for svg in svgs:
        assert svg.startswith("<svg") and 'class="cx"' in svg
        assert not HEX_COLOR.findall(svg)


def test_stylesheet_ships_the_chart_classes_for_both_themes():
    from bot.theme import chart_stylesheet, stylesheet

    sheet = stylesheet()
    for selector in (
        ".cx .pos{fill:var(--good)}", ".cx .neg{fill:var(--bad)}", ".tabs a[aria-current]",
        "table.heat td.cell.pos{--tint:var(--good)}", "table.heat td.cell.neg{--tint:var(--bad)}",
        "color-mix(in srgb,var(--tint,transparent) calc(var(--i,0)*55%),transparent)",
    ):
        assert selector in sheet
    standalone = chart_stylesheet()
    assert ".cx .pos{fill:var(--good)}" in standalone and "--good:" in standalone
    assert "aside.rail" not in standalone


def test_gamma_chart_html_threads_settings_theme():
    store = _seeded_store()
    body = _chart(store, "TEST", settings=DashboardSettings(theme="light")).body.decode("utf-8")
    assert 'data-theme="light"' in body


def test_board_rows_cover_every_underlying():
    rows = board_rows(_seeded_store())
    assert [row["symbol"] for row in rows] == ["DEMO", "TEST"]
    test = next(row for row in rows if row["symbol"] == "TEST")
    assert test["regime"] == "long-gamma"
    assert test["flip"] == 105.0
    assert test["flip_distance_pct"] == 5.0
    assert test["wall_strike"] == 95.0


def test_regime_board_html_renders_table():
    page = regime_board_html(_seeded_store())
    assert "<table>" in page
    assert "DEMO" in page and "TEST" in page
    assert "+5.0%" in page
    assert 'http-equiv="refresh"' in page


def test_regime_board_html_empty_store():
    assert "no snapshots stored" in regime_board_html(SnapshotStore())


def test_chart_route_serves_page_and_400s_unknown():
    response = route_http("GET", "/chart?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "gamma by strike" in response.body.decode("utf-8")
    missing = route_http("GET", "/chart?underlying=NOPE", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert missing.status == 400


def test_board_route_serves_page():
    response = route_http("GET", "/board", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "Dealer positioning board" in response.body.decode("utf-8")


def test_chart_and_board_have_ticker_search():
    store = _seeded_store()
    chart = route_http("GET", "/chart?underlying=TEST", {}, store, Journal(), CONFIG, NOW)
    body = chart.body.decode("utf-8")
    assert '<form class="block" method="get" action="/chart" role="search">' in body
    assert 'name="underlying"' in body
    assert '<option value="DEMO"></option>' in body
    assert '<option value="TEST"></option>' in body
    board = route_http("GET", "/board", {}, store, Journal(), CONFIG, NOW)
    assert 'action="/chart"' in board.body.decode("utf-8")


def test_chart_search_reaches_another_ticker_case_insensitively():
    store = _seeded_store()
    response = route_http("GET", "/chart?underlying=demo", {}, store, Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "DEMO gamma by strike" in response.body.decode("utf-8")


def test_root_is_portfolio_overview_not_a_symbol_dashboard():
    response = route_http("GET", "/", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "Portfolio overview" in body
    assert "Paper P&amp;L" in body
    assert "Trade record" in body
    assert "<h2>Holdings</h2>" in body
    assert "<h2>Digest</h2>" in body
    assert "Top card" not in body
    empty = route_http("GET", "/", {}, SnapshotStore(), Journal(), CONFIG, NOW)
    assert empty.status == 200
    assert "Portfolio overview" in empty.body.decode("utf-8")


def test_dashboard_has_no_video_section():
    response = route_http("GET", "/dashboard?underlying=TEST", {}, _seeded_store(), Journal(), CONFIG, NOW)
    assert response.status == 200
    assert "video" not in response.body.decode("utf-8").lower()
