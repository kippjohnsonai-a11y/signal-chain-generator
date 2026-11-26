"""Where the library lives, before and after the rename."""

from stompxl import config


def _on_a_mac(monkeypatch, home):
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.Path, "home", lambda: home)


def test_new_install_uses_the_new_name(monkeypatch, tmp_path):
    _on_a_mac(monkeypatch, tmp_path)
    assert config.base_dir() == tmp_path / "Library" / "Application Support" / "Signal Chain Generator"


def test_existing_library_stays_where_it_is(monkeypatch, tmp_path):
    legacy = tmp_path / "Library" / "Application Support" / "StompXL Tone Studio"
    legacy.mkdir(parents=True)
    _on_a_mac(monkeypatch, tmp_path)
    assert config.base_dir() == legacy
    assert not (tmp_path / "Library" / "Application Support" / "Signal Chain Generator").exists()


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "elsewhere"))
    assert config.base_dir() == tmp_path / "elsewhere"
