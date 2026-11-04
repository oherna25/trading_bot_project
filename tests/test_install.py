import tomllib
from pathlib import Path

import run_app
import run_demo
import run_session
from bot import video
from bot.config import read_dotenv

ROOT = Path(__file__).resolve().parent.parent


def test_console_scripts_resolve_and_discard_main_return(monkeypatch):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert scripts == {
        "marketwingman": "run_app:console_main",
        "marketwingman-demo": "run_demo:console_main",
        "marketwingman-session": "run_session:console_main",
    }
    monkeypatch.setattr(run_app, "main", lambda argv=None: "brief text")
    monkeypatch.setattr(run_demo, "main", lambda argv=None: ["SPY"])
    monkeypatch.setattr(run_session, "main", lambda argv=None: "brief text")
    assert run_app.console_main() is None
    assert run_demo.console_main() is None
    assert run_session.console_main() is None


def test_windows_gets_tzdata_dependency():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == ["tzdata; sys_platform == 'win32'"]
    assert pyproject["tool"]["setuptools"]["packages"] == ["bot"]
    assert pyproject["tool"]["setuptools"]["py-modules"] == ["run_app", "run_demo", "run_session"]


def test_read_dotenv_handles_utf8(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GREETING=café — SPY\n", encoding="utf-8")
    assert read_dotenv(str(path)) == {"GREETING": "café — SPY"}


def test_render_video_runs_resolved_npx_path(monkeypatch):
    seen = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(video.shutil, "which", lambda _name: "C:\\node\\npx.cmd")
    monkeypatch.setattr(video.subprocess, "run", lambda argv, **kw: seen.update(argv=argv) or Done())
    result = video.render_video("proj", "brief.mp4")
    assert result.ok is True
    assert seen["argv"][0] == "C:\\node\\npx.cmd"
