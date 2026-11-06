import logging

import pytest

from traceloom.config import Settings
from traceloom.logging_setup import HANDLER_NAME, ensure_console_logging
from traceloom.services.mail import Mailer


@pytest.fixture
def bare_logging(monkeypatch):
    """Call it inside the test: the state under `uvicorn traceloom.main:app`, with no root
    handlers and none for traceloom. (pytest installs its own root handlers around each test
    call, so this has to happen after that.)"""

    def reset() -> logging.Logger:
        ours = logging.getLogger("traceloom")
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        monkeypatch.setattr(ours, "handlers", [])
        monkeypatch.setattr(ours, "level", logging.NOTSET)
        return ours

    return reset


def test_console_mail_is_visible_by_default(bare_logging, capsys):
    bare_logging()
    ensure_console_logging()
    mailer = Mailer(Settings(TRACELOOM_ENV="test", TRACELOOM_SERVER_SECRET="x" * 20))
    mailer.send(
        "a@example.com",
        "Reset your password",
        "Open http://localhost:8000/reset/abc123 to continue.",
    )
    err = capsys.readouterr().err
    assert "a@example.com" in err and "http://localhost:8000/reset/abc123" in err
    assert "Reset your password" in err


def test_it_is_installed_once(bare_logging):
    ours = bare_logging()
    ensure_console_logging()
    ensure_console_logging()
    assert [h.get_name() for h in ours.handlers] == [HANDLER_NAME]


def test_it_leaves_an_existing_logging_setup_alone(monkeypatch):
    root, ours = logging.getLogger(), logging.getLogger("traceloom")
    monkeypatch.setattr(root, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(ours, "handlers", [])
    ensure_console_logging()
    assert ours.handlers == []


def test_app_startup_installs_it(bare_logging, settings):
    from traceloom.main import create_app

    ours = bare_logging()
    create_app(settings)
    assert [h.get_name() for h in ours.handlers] == [HANDLER_NAME]
