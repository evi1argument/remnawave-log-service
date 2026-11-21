import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == "posix", "The installer targets Linux")
class SetupMenuTest(unittest.TestCase):
    def run_menu(self, *arguments, input_text=""):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(["bash", str(root / "install.sh"), *arguments],
                              input=input_text, text=True, capture_output=True, timeout=12)

    def test_menu_lists_supported_actions_and_exits_without_changes(self):
        result = self.run_menu(input_text="0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install a collector", result.stdout)
        self.assertIn("Set up availability monitoring", result.stdout)
        self.assertIn("Configure Telegram alerts", result.stdout)
        self.assertIn("SERVER", result.stdout)
        self.assertNotIn("\x1b", result.stdout)

    def test_eof_exits_without_changes(self):
        self.assertEqual(self.run_menu().returncode, 0)

    def test_unknown_action_fails_with_usage(self):
        result = self.run_menu("unexpected")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_help_mentions_role_specific_hosts(self):
        result = self.run_menu("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("respective hosts", result.stdout)
        self.assertIn("notify status|test|disable", result.stdout)

    def test_version_matches_the_release_file(self):
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        result = self.run_menu("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"Remnawave Log Service {version}")

    def test_switches_languages_and_returns_to_english(self):
        result = self.run_menu(input_text="l\nЛ\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Install a collector"), 2)
        self.assertIn("Установить коллектор", result.stdout)
        self.assertIn("Выберите действие", result.stderr)

    def test_direct_russian_diagnostics_and_unknown_language(self):
        result = self.run_menu("--lang", "ru", "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Диагностика", result.stdout)
        self.assertIn("Ресурсы", result.stdout)
        self.assertEqual(self.run_menu("--lang", "de", "doctor").returncode, 2)
        self.assertEqual(self.run_menu("--lang").returncode, 2)
        self.assertEqual(self.run_menu("--lang", "en", "--lang", "ru").returncode, 2)
        self.assertIn("Диагностика", self.run_menu("--lang=ru", "doctor").stdout)

    def test_terminal_navigates_and_restores_the_screen(self):
        import errno
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time

        root = Path(__file__).resolve().parents[1]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 26, 56, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "INSTALL_LANG": "en"}
        env.pop("NO_COLOR", None)
        process = subprocess.Popen(["bash", str(root / "install.sh")], stdin=slave, stdout=slave,
                                   stderr=slave, env=env, cwd=root)
        os.close(slave)
        buffer = bytearray()

        def wait_for(marker):
            end = time.monotonic() + 5
            while marker not in buffer and time.monotonic() < end:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    buffer.extend(os.read(master, 8192))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
            self.assertIn(marker, buffer)

        try:
            wait_for(b"Enter Open")
            self.assertNotIn(b"Loki, Grafana, archive", buffer)
            os.write(master, b"L")
            wait_for("Enter Открыть".encode())
            os.write(master, b"\x1b[A\x1b[A\n")
            wait_for("Диагностика".encode())
            self.assertIn(b"\x1b[?1049l", buffer)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()
