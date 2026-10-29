from bot.muse import default_dashboard_base_url


def test_dashboard_base_url_prefers_process_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DASHBOARD_BASE_URL=https://file.example.internal\n")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://env.example.internal")
    assert default_dashboard_base_url() == "https://env.example.internal"


def test_dashboard_base_url_falls_back_to_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    (tmp_path / ".env").write_text("DASHBOARD_BASE_URL=https://file.example.internal\n")
    assert default_dashboard_base_url() == "https://file.example.internal"


def test_dashboard_base_url_defaults_without_env_or_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert default_dashboard_base_url() == "http://127.0.0.1:8000"


def test_dashboard_base_url_default_follows_port(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.setenv("PORT", "9002")
    assert default_dashboard_base_url() == "http://127.0.0.1:9002"


def test_dashboard_base_url_explicit_env_mapping(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert default_dashboard_base_url({}) == "http://127.0.0.1:8000"
    assert (
        default_dashboard_base_url({"DASHBOARD_BASE_URL": "  https://mapped.example  "})
        == "https://mapped.example"
    )
