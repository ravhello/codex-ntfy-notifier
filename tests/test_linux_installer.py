from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-linux.sh"
TARGET_INSTALLER = ROOT / "src" / "install-remote-linux-target.py"
NOTIFIER = ROOT / "src" / "notify-ntfy.py"


@unittest.skipIf(sys.platform == "win32", "native Linux installer test")
class LinuxInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex ntfy installer ")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_installer(
        self,
        codex_home: Path,
        *,
        topic: str | None = "test-topic",
        skip_systemd: bool = True,
        home: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        if skip_systemd:
            environment["CODEX_NTFY_SKIP_SYSTEMD"] = "1"
        else:
            environment.pop("CODEX_NTFY_SKIP_SYSTEMD", None)
        if home is not None:
            environment["HOME"] = str(home)
        if topic is None:
            environment.pop("CODEX_NTFY_TOPIC", None)
        else:
            environment["CODEX_NTFY_TOPIC"] = topic
        return subprocess.run(
            ["sh", str(INSTALLER)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_fresh_install_and_upgrade_from_path_with_spaces(self) -> None:
        codex_home = self.root / "home with spaces" / ".codex"
        first = self.run_installer(codex_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        details = json.loads(first.stdout)
        self.assertTrue(details["topic_configured"])
        self.assertEqual(details["worker"], "on-demand")
        config = json.loads((codex_home / "ntfy-config.json").read_text(encoding="utf-8"))
        self.assertFalse(config["include_message"])
        self.assertIn("notify-ntfy.py", (codex_home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE((codex_home / "ntfy-config.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((codex_home / "notify-ntfy.py").stat().st_mode), 0o700)

        second = self.run_installer(codex_home, topic=None)
        self.assertEqual(second.returncode, 0, second.stderr)
        backups = [path for path in (codex_home / "ntfy-backups").iterdir() if path.is_dir()]
        self.assertGreaterEqual(len(backups), 2)

    def test_unrelated_notify_hook_causes_complete_rollback(self) -> None:
        codex_home = self.root / "conflict" / ".codex"
        codex_home.mkdir(parents=True)
        original = 'notify = ["other-hook"]\n\n[model]\n'
        (codex_home / "config.toml").write_text(original, encoding="utf-8")

        result = self.run_installer(codex_home)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated notify", result.stderr)
        self.assertEqual((codex_home / "config.toml").read_text(encoding="utf-8"), original)
        self.assertFalse((codex_home / "notify-ntfy.py").exists())
        self.assertFalse((codex_home / "ntfy-config.json").exists())

    def test_nested_notify_key_does_not_mask_required_root_hook(self) -> None:
        codex_home = self.root / "nested" / ".codex"
        codex_home.mkdir(parents=True)
        nested = '[tools]\nnotify = ["nested-tool-hook"]\n'
        (codex_home / "config.toml").write_text(nested, encoding="utf-8")

        result = self.run_installer(codex_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("notify = ["))
        self.assertIn(nested, text)
        self.assertEqual(text.count("notify = ["), 2)

    def test_legacy_bom_config_preserves_content_and_title_behavior(self) -> None:
        codex_home = self.root / "legacy" / ".codex"
        codex_home.mkdir(parents=True)
        legacy = {
            "server": "https://ntfy.sh",
            "topic": "legacy-test-topic",
        }
        (codex_home / "ntfy-config.json").write_text(json.dumps(legacy), encoding="utf-8-sig")

        result = self.run_installer(codex_home, topic=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        migrated = json.loads((codex_home / "ntfy-config.json").read_text(encoding="utf-8"))
        self.assertTrue(migrated["include_message"])
        self.assertTrue(migrated["include_thread_title"])
        self.assertFalse(migrated["allow_insecure_auth"])
        self.assertEqual(migrated["dead_retention_days"], 30)

    def test_unrelated_systemd_unit_is_never_overwritten(self) -> None:
        home = self.root / "linux home"
        unit = home / ".config" / "systemd" / "user" / "codex-ntfy.service"
        unit.parent.mkdir(parents=True)
        original = "[Service]\nExecStart=/usr/bin/unrelated-service\n"
        unit.write_text(original, encoding="utf-8")
        codex_home = home / ".codex"

        result = self.run_installer(codex_home, skip_systemd=False, home=home)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated systemd unit", result.stderr)
        self.assertEqual(unit.read_text(encoding="utf-8"), original)
        self.assertFalse((codex_home / "notify-ntfy.py").exists())

    def test_staging_hash_mismatch_rolls_back_without_cutover(self) -> None:
        codex_home = self.root / "hash mismatch" / ".codex"
        stage = codex_home / ".stage"
        stage.mkdir(parents=True)
        shutil.copy2(NOTIFIER, stage / "notify-ntfy.py")
        shutil.copy2(TARGET_INSTALLER, stage / "install-remote-linux-target.py")
        (stage / "ntfy-config.json").write_text(
            json.dumps({"server": "https://ntfy.sh", "topic": "test-topic"}),
            encoding="utf-8",
        )
        environment = {**os.environ, "CODEX_HOME": str(codex_home)}
        bad_hash = "0" * 64
        result = subprocess.run(
            [
                sys.executable,
                str(TARGET_INSTALLER),
                "--origin",
                "test",
                "--skip-systemd",
                "--stage-dir",
                str(stage),
                "--expected-sha256",
                f"notify-ntfy.py={bad_hash}",
                "--expected-sha256",
                f"install-remote-linux-target.py={bad_hash}",
                "--expected-sha256",
                f"ntfy-config.json={bad_hash}",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)
        self.assertFalse((codex_home / "notify-ntfy.py").exists())
        self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
