from __future__ import annotations

from datetime import datetime

from bot.diff import compact_dollars
from bot.exposure import compute_gex
from bot.journal import Journal
from bot.store import SnapshotStore, need_snapshot_text


def _paper_net(journal: Journal, symbol: str) -> float:
    from bot.research import open_trade_size

    return open_trade_size(journal, symbol)


def explain_position(symbol: str, store: SnapshotStore, journal: Journal, now: datetime) -> str:
    from bot.holdings import describe_holding

    clean = (symbol or "").strip().upper()
    if not clean:
        return "explain needs a symbol -- next: chat explain my TEST"
    holdings = [h for h in store.read_holdings() if h.symbol == clean]
    net = _paper_net(journal, clean)
    if not holdings and net == 0:
        return (
            f"no position in {clean} -- next: save holding long 100 {clean} stock "
            "or take a paper trade via take_paper_trade"
        )
    found = store.read_latest(clean)
    if found is None:
        return need_snapshot_text(clean, store)
    snapshot_id, snapshot = found
    bar = store.read_latest_bar(clean)
    spot = bar.close if bar is not None else None
    profile = compute_gex(snapshot, snapshot_id, spot=spot)
    regime = "long-gamma" if profile.total_exposure >= 0.0 else "short-gamma"
    spot_text = f"{spot:g}" if spot is not None else "n/a"
    flip_text = f"{profile.flip_point:g}" if profile.flip_point is not None else "none"
    walls = sorted(profile.per_strike_exposure.items(), key=lambda item: -abs(item[1]))[:2]
    if walls:
        wall_bit = "; ".join(f"{strike:g} {compact_dollars(value)}" for strike, value in walls)
    else:
        wall_bit = "no walls"
    brief_bit = (
        f"brief reads {regime} with spot {spot_text} vs flip {flip_text} "
        f"and {compact_dollars(profile.total_exposure)} total"
    )
    try:
        from bot.wheel import wheel_response

        wheel_text = wheel_response(f"wheel {clean}", clean, store, journal, now)
    except ValueError as exc:
        wheel_bit = str(exc).replace("\n", " ")
    else:
        top = ""
        for line in wheel_text.splitlines():
            if "SELL_PUT" in line or "SELL_CALL" in line:
                top = " ".join(line.split()[1:])
                break
        if top:
            wheel_bit = f"wheel top {top}"
        elif "none found" in wheel_text:
            wheel_bit = "wheel finds no premium ideas"
        else:
            wheel_bit = " ".join(wheel_text.split())
    if store.read_profile() is None:
        debate_bit = "debate needs a profile"
    else:
        try:
            from bot.debate import debate_card
            from bot.demo import demo_cards

            _, cards = demo_cards(clean, store, journal, now)
            if not cards:
                debate_bit = "debate finds no card"
            else:
                debate_text = debate_card(cards[0], journal, now)
                verdict = debate_text.splitlines()[-1] if debate_text else "verdict unknown"
                debate_bit = f"debate says {verdict}"
        except ValueError as exc:
            debate_bit = str(exc).replace("\n", " ")
    bits = [describe_holding(h) for h in holdings]
    if net != 0:
        bits.append(f"paper {net:g} {clean} shares")
    shares_bit = ", ".join(bits) if bits else f"paper {net:g} {clean} shares"
    paragraph = (
        f"Your {shares_bit} sit in {regime}: {brief_bit}; "
        f"walls {wall_bit}; {wheel_bit}; {debate_bit}."
    )
    paragraph = " ".join(paragraph.split())
    journal.append(now, "position_explained", {"symbol": clean, "text": paragraph})
    return paragraph
