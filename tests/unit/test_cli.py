import pytest

from github_bot.cli import main


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "github-bot" in capsys.readouterr().out
