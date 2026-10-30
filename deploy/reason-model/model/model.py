"""v0 reason composer for trade cards.

Deterministic template the operator swaps for an LLM. Reads the same
stat fields the app fallback uses and returns one sentence per card.
"""


def compose_reason(stats):
    stats = stats if isinstance(stats, dict) else {}
    parts = [f"{stats.get('direction', '')} via {stats.get('spread', '')}"]
    wall = stats.get("wall_exposure")
    if wall is not None:
        parts.append(f"wall exposure {wall:.0f}")
    flip = stats.get("flip_point")
    spot = stats.get("spot")
    if flip is not None and spot is not None:
        side = "above" if spot > flip else "below"
        parts.append(f"spot {spot:.1f} {side} flip {flip:.1f}")
    elif flip is not None:
        parts.append(f"flip {flip:.1f}")
    max_loss = stats.get("max_loss")
    if max_loss is not None:
        parts.append(f"capped loss ${max_loss:.0f}")
    breakeven = stats.get("breakeven")
    if breakeven is not None:
        parts.append(f"breakeven {breakeven:.1f}")
    return ", ".join(parts) + "."


class Model:
    def load(self):
        pass

    def predict(self, request):
        if isinstance(request, dict) and "stats" in request:
            return {"reason": compose_reason(request["stats"])}
        if isinstance(request, dict) and "cards" in request:
            cards = request["cards"] or []
            return {"reasons": [compose_reason(card) for card in cards]}
        raise ValueError("predict needs {stats: {...}} or {cards: [{...}, ...]}")
