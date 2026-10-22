import subprocess
from pathlib import Path

import pytest


class GitRepo:
    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@example.com")
        self.git("config", "user.name", "ci")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> GitRepo:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return GitRepo(tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_github_event(monkeypatch):
    """Tests must not pick up the event of the CI run executing them (its base commit
    does not exist in a test repository)."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
