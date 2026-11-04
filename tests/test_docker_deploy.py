from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_serves_demo_by_default_and_stays_unprivileged():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER app" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/healthz" in dockerfile
    assert "COPY docs/icons" in dockerfile
    assert 'CMD ["sh", "-c", "exec marketwingman-demo' in dockerfile


def test_compose_deploys_app_demo_and_session_on_named_volumes():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in ("  app:", "  demo:", "  session:"):
        assert service in compose
    assert "- marketwingman\n" in compose
    assert "- marketwingman-session" in compose
    assert "/data/trading-bot" in compose
    assert "app-data:/data" in compose and "demo-data:/data" in compose
    assert "LEDGER_DIR: /data/ledger" in compose
    assert compose.count("required: false") == 3
