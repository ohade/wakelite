import subprocess
import sys


def test_timer_create_help_documents_current_callback_contract():
    result = subprocess.run(
        [sys.executable, "-m", "wakelite.cli", "timer", "create", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"cmux", "ghostty", or "wezterm"' in result.stdout
    assert '"amq": true' in result.stdout
    assert "cmux+session defaults true at creation" in result.stdout
    assert 'only "wezterm" supported' not in result.stdout
