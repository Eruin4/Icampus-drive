"""Isolated plugin tests: no credentials, live HTTP, or collector subprocesses.

Trimmed from the author's Hermes-tree conftest: the calendar fixtures were
dropped together with the calendar modules, which are not part of this repo.
"""
import socket
import subprocess
from unittest.mock import Mock

import pytest
import requests

from plugins.icampus import icampus


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(icampus, "PROJECT", home)
    return home


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Assertions inside judge() can be caught as fallbacks; also check calls
    # at teardown so an accidental real transport cannot silently pass.
    blocked = Mock(side_effect=AssertionError("unmocked network/subprocess call"))
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(requests.sessions.Session, "send", blocked)
    monkeypatch.setattr(subprocess, "run", blocked)
    yield
    blocked.assert_not_called()


@pytest.fixture
def notice():
    return {"course_id": 7, "notice_id": 11, "kind": "announcement",
            "course_name": "운영체제", "title": "시험 안내",
            "body": "2026-10-20 시험, 2026-10-21 과제 마감",
            "created_at": "2026-10-01T09:00:00+09:00", "updated_at": "v2",
            "url": "https://canvas.skku.edu/courses/7/announcements/11"}
