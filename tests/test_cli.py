import sys

import pytest
from conftest import EXAMPLE

from permissions_ui import __main__ as cli


def test_check_passes_the_example_catalog(capsys):
    assert cli.check(EXAMPLE) == 0
    assert "the catalog holds together" in capsys.readouterr().out


def test_check_names_every_problem(tmp_path, capsys):
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "r.yaml").write_text("- id: r\n  name: R\n  description: x\n  resources: [missing]\n")
    assert cli.check(tmp_path) == 1
    assert "role r: resource missing is not a resource" in capsys.readouterr().err


def test_an_unknown_command_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["permissions-ui", "dance"])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 2


def test_check_from_the_command_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["permissions-ui", "check", str(EXAMPLE)])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 0


def test_a_bad_configuration_stops_the_server(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["permissions-ui"])
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 1 and "configuration refused" in capsys.readouterr().err
