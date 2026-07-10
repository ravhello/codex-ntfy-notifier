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
        extra_env: dict[str, str] | None = None,
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
        if extra_env:
            environment.update(extra_env)
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
        self.assertEqual(config["idle_detection_mode"], "strict")
        self.assertEqual(config["idle_grace_seconds"], 1.5)
        self.assertTrue(config["goal_aware"])
        self.assertTrue(config["watch_rollouts"])
        self.assertEqual(config["watch_discovery_seconds"], 60)
        self.assertEqual(config["watch_roots"], [])
        self.assertIn("notify-ntfy.py", (codex_home / "config.toml").read_text(encoding="utf-8"))
        hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(set(hooks["hooks"]), {"Stop"})
        hook_command = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("notify-ntfy.py", hook_command)
        self.assertIn("--hook-event", hook_command)
        self.assertIn("/hooks", first.stderr)
        self.assertEqual(stat.S_IMODE((codex_home / "ntfy-config.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((codex_home / "hooks.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((codex_home / "notify-ntfy.py").stat().st_mode), 0o700)

        second = self.run_installer(codex_home, topic=None)
        self.assertEqual(second.returncode, 0, second.stderr)
        backups = [path for path in (codex_home / "ntfy-backups").iterdir() if path.is_dir()]
        self.assertGreaterEqual(len(backups), 2)

    def test_hooks_merge_removes_only_managed_handlers_and_is_idempotent(self) -> None:
        codex_home = self.root / "hooks merge" / ".codex"
        codex_home.mkdir(parents=True)
        original_hooks = {
            "metadata": {"owner": "someone-else"},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "/opt/foreign/pre-tool"},
                            {"type": "command", "command": "/opt/notify-ntfy-helper.exe"},
                            {"type": "command", "command": "/old/notify-ntfy.py --hook-event"},
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "/old/notify-ntfy.py --prompt"}]}
                ],
                "SubagentStop": [
                    {"hooks": [{"type": "command", "command": "/old/notify-ntfy.py --subagent"}]}
                ],
                "Stop": [
                    {
                        "foreignProperty": "keep-me",
                        "hooks": [
                            {"type": "command", "command": "/opt/foreign/stop"},
                            {"type": "command", "command": "/old/notify-ntfy.py --stop"},
                        ],
                    },
                    {"hooks": [{"type": "command", "command": "/opt/foreign/second-stop"}]},
                ],
            },
        }
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text(json.dumps(original_hooks, indent=2) + "\n", encoding="utf-8")

        first = self.run_installer(codex_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        installed_text = hooks_path.read_text(encoding="utf-8")
        installed = json.loads(installed_text)
        self.assertEqual(installed["metadata"], original_hooks["metadata"])
        self.assertNotIn("UserPromptSubmit", installed["hooks"])
        self.assertNotIn("SubagentStop", installed["hooks"])

        commands_by_event: dict[str, list[str]] = {}
        for event_name, groups in installed["hooks"].items():
            commands_by_event[event_name] = [
                handler["command"]
                for group in groups
                if isinstance(group, dict)
                for handler in group.get("hooks", [])
                if isinstance(handler, dict) and isinstance(handler.get("command"), str)
            ]
        self.assertEqual(
            commands_by_event["PreToolUse"],
            ["/opt/foreign/pre-tool", "/opt/notify-ntfy-helper.exe"],
        )
        self.assertIn("/opt/foreign/stop", commands_by_event["Stop"])
        self.assertIn("/opt/foreign/second-stop", commands_by_event["Stop"])
        managed = [
            (event_name, command)
            for event_name, commands in commands_by_event.items()
            for command in commands
            if "notify-ntfy" in command and command != "/opt/notify-ntfy-helper.exe"
        ]
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0][0], "Stop")
        self.assertIn("--hook-event", managed[0][1])
        self.assertEqual(installed["hooks"]["Stop"][0]["foreignProperty"], "keep-me")

        second = self.run_installer(codex_home, topic=None)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), installed_text)

    def test_hooks_are_restored_when_post_merge_doctor_fails(self) -> None:
        codex_home = self.root / "hook rollback" / ".codex"
        stage = codex_home / ".stage"
        stage.mkdir(parents=True)
        original_config = "[model]\nname = \"foreign\"\n"
        original_hooks = json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/opt/foreign/stop"}]}
                    ]
                }
            },
            indent=2,
        ) + "\n"
        (codex_home / "config.toml").write_text(original_config, encoding="utf-8")
        (codex_home / "hooks.json").write_text(original_hooks, encoding="utf-8")
        (stage / "notify-ntfy.py").write_text(
            "import sys\nraise SystemExit(17 if '--doctor' in sys.argv else 0)\n",
            encoding="utf-8",
        )
        shutil.copy2(TARGET_INSTALLER, stage / "install-remote-linux-target.py")
        (stage / "ntfy-config.json").write_text(
            json.dumps({"server": "https://ntfy.sh", "topic": "test-topic"}),
            encoding="utf-8",
        )
        environment = {**os.environ, "CODEX_HOME": str(codex_home)}

        result = subprocess.run(
            [
                sys.executable,
                str(TARGET_INSTALLER),
                "--origin",
                "test",
                "--skip-systemd",
                "--stage-dir",
                str(stage),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((codex_home / "config.toml").read_text(encoding="utf-8"), original_config)
        self.assertEqual((codex_home / "hooks.json").read_text(encoding="utf-8"), original_hooks)
        self.assertFalse((codex_home / "notify-ntfy.py").exists())
        backups = sorted(path for path in (codex_home / "ntfy-backups").iterdir() if path.is_dir())
        self.assertEqual((backups[-1] / "hooks.json").read_text(encoding="utf-8"), original_hooks)
        self.assertEqual(stat.S_IMODE((backups[-1] / "hooks.json").stat().st_mode), 0o600)

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
            "watch_roots": [{"path": r"\\wsl.localhost\Source\home\user\.codex"}],
        }
        (codex_home / "ntfy-config.json").write_text(json.dumps(legacy), encoding="utf-8-sig")

        result = self.run_installer(codex_home, topic=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        migrated = json.loads((codex_home / "ntfy-config.json").read_text(encoding="utf-8"))
        self.assertTrue(migrated["include_message"])
        self.assertTrue(migrated["include_thread_title"])
        self.assertFalse(migrated["allow_insecure_auth"])
        self.assertEqual(migrated["idle_detection_mode"], "strict")
        self.assertEqual(migrated["idle_grace_seconds"], 1.5)
        self.assertEqual(migrated["idle_probe_grace_seconds"], 30)
        self.assertTrue(migrated["goal_aware"])
        self.assertEqual(migrated["goal_poll_seconds"], 1)
        self.assertEqual(migrated["subagent_orphan_seconds"], 1800)
        self.assertTrue(migrated["suppress_technical_turns"])
        self.assertTrue(migrated["watch_rollouts"])
        self.assertEqual(migrated["watch_scan_seconds"], 2)
        self.assertEqual(migrated["watch_discovery_seconds"], 60)
        self.assertEqual(migrated["watch_initial_replay_seconds"], 15)
        self.assertEqual(migrated["watch_roots"], [])
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

    def test_systemd_worker_preserves_distinct_sqlite_home(self) -> None:
        home = self.root / "systemd home"
        codex_home = home / "custom codex"
        sqlite_home = home / "custom sqlite"
        sqlite_home.mkdir(parents=True)
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *' is-enabled '*' --quiet '*) exit 1 ;;\n"
            "  *' is-active '*' --quiet '*) exit 1 ;;\n"
            "  *' is-active '*) printf 'active\\n'; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o700)
        result = self.run_installer(
            codex_home,
            skip_systemd=False,
            home=home,
            extra_env={
                "CODEX_SQLITE_HOME": str(sqlite_home),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = home / ".config" / "systemd" / "user" / "codex-ntfy.service"
        text = unit.read_text(encoding="utf-8")
        self.assertIn(f'Environment="CODEX_HOME={codex_home}"', text)
        self.assertIn(f'Environment="CODEX_SQLITE_HOME={sqlite_home}"', text)

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
