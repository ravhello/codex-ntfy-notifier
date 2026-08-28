from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from base64 import b64encode
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET_INSTALLER = ROOT / "src" / "install-remote-windows-target.ps1"
WINDOWS_POWERSHELL = (
    Path(os.environ.get("WINDIR", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
UTF8_BOM = b"\xef\xbb\xbf"


@unittest.skipUnless(
    os.name == "nt" and WINDOWS_POWERSHELL.exists(),
    "Windows PowerShell 5.1 remote installer test",
)
class RemoteWindowsInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex ntfy remote windows ")
        self.home = Path(self.temp.name)
        shutil.copy2(TARGET_INSTALLER, self.home / TARGET_INSTALLER.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _encode_json(value: object, *, bom: bool = False) -> bytes:
        text = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").replace(
            "\n", "\r\n"
        )
        encoded = text.encode("utf-8")
        return (UTF8_BOM + encoded) if bom else encoded

    def _write_fixture(self, *, bom: bool = False) -> None:
        notifier = """[CmdletBinding()]
param([switch]$Doctor)
$ErrorActionPreference = 'Stop'
if (-not $Doctor) { throw 'The fixture supports doctor mode only.' }
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
[pscustomobject][ordered]@{ topic_configured = $true } | ConvertTo-Json -Compress
"""
        notifier_bytes = notifier.encode("utf-8")
        if bom:
            notifier_bytes = UTF8_BOM + notifier_bytes
        (self.home / "notify-ntfy.ps1").write_bytes(notifier_bytes)
        (self.home / "watch-codex-ntfy.ps1").write_bytes(
            (UTF8_BOM if bom else b"") + b"# UTF-8 fixture only\r\n"
        )

        private_config = {
            "server": "https://ntfy.sh",
            "topic": "test-topic",
            "username": "Renée d'Italia",
            "password": 'pàss-"quoted"-\\slash-😀',
            "custom_metadata": "Caffè, città e l’apostrofo",
        }
        (self.home / "ntfy-config.json").write_bytes(
            self._encode_json(private_config, bom=bom)
        )

        hooks = {
            "metadata": {
                "owner": "Renée",
                "description": 'Caffè "speciale" \\ 😀',
            },
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'foreign-tool --label "città"',
                            }
                        ],
                    }
                ]
            },
        }
        (self.home / "hooks.json").write_bytes(self._encode_json(hooks, bom=bom))

        config = (
            '# Caffè, città, apostrofo l’Italia, quote "ok", slash \\\n'
            '[projects."C:\\\\work\\\\città"]\n'
            'trusted = true\n'
        ).replace("\n", "\r\n")
        config_bytes = config.encode("utf-8")
        if bom:
            config_bytes = UTF8_BOM + config_bytes
        (self.home / "config.toml").write_bytes(config_bytes)

        vbs_bytes = b"' UTF-8 fixture only\r\n"
        if bom:
            vbs_bytes = UTF8_BOM + vbs_bytes
        (self.home / "watch-codex-ntfy-hidden.vbs").write_bytes(vbs_bytes)

    def _run_installer(
        self,
        *,
        skip_scheduled_task: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.home / TARGET_INSTALLER.name),
        ]
        if skip_scheduled_task:
            command.append("-SkipScheduledTask")
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        return subprocess.run(
            command,
            cwd=self.home,
            capture_output=True,
            timeout=60,
            check=False,
            env=process_environment,
        )

    @staticmethod
    def _details(process: subprocess.CompletedProcess[bytes]) -> str:
        return (process.stdout + b"\n" + process.stderr).decode("utf-8", errors="replace")

    def _assert_private_acls(self, paths: list[Path]) -> None:
        file_paths = [path for path in paths if path.is_file()]
        directory_paths = [path for path in paths if path.is_dir()]
        quoted_files = ",".join(
            "'" + str(path).replace("'", "''") + "'" for path in file_paths
        )
        quoted_directories = ",".join(
            "'" + str(path).replace("'", "''") + "'" for path in directory_paths
        )
        command = (
            "$ErrorActionPreference='Stop'; "
            "$allowed=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,"
            "'S-1-5-18','S-1-5-32-544'); "
            "$check={ param($acl,$path); if(-not $acl.AreAccessRulesProtected){"
            "throw ('ACL inheritance remains enabled: '+$path)}; "
            "$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); "
            "if($rules.Count -ne 3){throw ('Unexpected ACE count: '+$path+' '+$rules.Count)}; "
            "foreach($rule in $rules){if($rule.AccessControlType -ne "
            "[Security.AccessControl.AccessControlType]::Allow -or "
            "$allowed -notcontains $rule.IdentityReference.Value -or "
            "($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) "
            "-ne [Security.AccessControl.FileSystemRights]::FullControl){"
            "throw ('Unexpected ACE: '+$path+' '+$rule.IdentityReference.Value)}}; "
            "foreach($sid in $allowed){if($rules.IdentityReference.Value -notcontains $sid){"
            "throw ('Missing ACE: '+$path+' '+$sid)}} }; "
            "@(" + quoted_files + ") | ForEach-Object { "
            "& $check ([IO.File]::GetAccessControl($_)) $_ }; @("
            + quoted_directories
            + ") | ForEach-Object { "
            "& $check ([IO.Directory]::GetAccessControl($_)) $_ }"
        )
        checked = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, self._details(checked))

    def _add_hostile_explicit_acls(self, paths: list[Path]) -> None:
        process_environment = os.environ.copy()
        process_environment["CODEX_NTFY_TEST_ACL_PATHS"] = "|".join(map(str, paths))
        command = r"""
$ErrorActionPreference='Stop'
$sids=@('S-1-1-0','S-1-5-32-545')
foreach($path in $env:CODEX_NTFY_TEST_ACL_PATHS.Split('|')) {
  $acl=[IO.File]::GetAccessControl($path)
  foreach($value in $sids) {
    $sid=New-Object Security.Principal.SecurityIdentifier($value)
    $rule=New-Object Security.AccessControl.FileSystemAccessRule(
      $sid,
      [Security.AccessControl.FileSystemRights]::Read,
      [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
  }
  [IO.File]::SetAccessControl($path,$acl)
}
"""
        changed = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            timeout=30,
            check=False,
            env=process_environment,
        )
        self.assertEqual(changed.returncode, 0, self._details(changed))

    def _tree_snapshot(self) -> dict[str, tuple[str, bytes | str]]:
        snapshot: dict[str, tuple[str, bytes | str]] = {}
        paths = [self.home, *sorted(self.home.rglob("*"))]
        process_environment = os.environ.copy()
        process_environment["CODEX_NTFY_TEST_SNAPSHOT_ROOT"] = str(self.home)
        command = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$root=[IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_SNAPSHOT_ROOT).TrimEnd('\')
$items=@(Get-Item -LiteralPath $root -Force)+@(Get-ChildItem -LiteralPath $root -Force -Recurse | Sort-Object FullName)
$result=@()
foreach($item in $items) {
  $relative=if($item.FullName -eq $root){'.'}else{$item.FullName.Substring($root.Length).TrimStart('\')}
  $acl=if($item.PSIsContainer){[IO.Directory]::GetAccessControl($item.FullName)}else{[IO.File]::GetAccessControl($item.FullName)}
  $result += [pscustomobject][ordered]@{ relative=$relative; sddl=$acl.Sddl }
}
$result | ConvertTo-Json -Depth 3 -Compress
"""
        acl_process = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            timeout=30,
            check=False,
            env=process_environment,
        )
        self.assertEqual(acl_process.returncode, 0, self._details(acl_process))
        acl_items = json.loads(acl_process.stdout.decode("utf-8"))
        if isinstance(acl_items, dict):
            acl_items = [acl_items]
        acls = {item["relative"]: item["sddl"] for item in acl_items}
        for path in paths:
            relative = "." if path == self.home else str(path.relative_to(self.home))
            if path.is_dir():
                snapshot[relative] = ("directory", acls[relative])
            else:
                snapshot[relative] = (
                    "file",
                    b64encode(path.read_bytes()).decode("ascii") + "|" + acls[relative],
                )
        return snapshot

    def _assert_no_transaction_artifacts(self) -> None:
        artifacts = [
            path
            for path in self.home.rglob("*")
            if path.name.startswith(".")
            and path.suffix in {".tmp", ".rollback", ".failed"}
        ]
        self.assertEqual(artifacts, [])

    def test_utf8_bom_accents_and_special_characters_are_preserved_atomically(self) -> None:
        self._write_fixture(bom=True)
        self._add_hostile_explicit_acls(
            [
                self.home / "ntfy-config.json",
                self.home / "hooks.json",
                self.home / "config.toml",
            ]
        )

        first = self._run_installer()
        self.assertEqual(first.returncode, 0, self._details(first))
        self.assertTrue((self.home / "notify-ntfy.ps1").read_bytes().startswith(UTF8_BOM))
        self.assertTrue((self.home / "watch-codex-ntfy.ps1").read_bytes().startswith(UTF8_BOM))
        self.assertTrue((self.home / "watch-codex-ntfy-hidden.vbs").read_bytes().startswith(UTF8_BOM))

        private_bytes = (self.home / "ntfy-config.json").read_bytes()
        hooks_bytes = (self.home / "hooks.json").read_bytes()
        config_bytes = (self.home / "config.toml").read_bytes()
        for rendered in (private_bytes, hooks_bytes, config_bytes):
            self.assertFalse(rendered.startswith(UTF8_BOM))
            rendered.decode("utf-8", errors="strict")
            self.assertNotIn(b"\xef\xbf\xbd", rendered)
            self.assertIn(b"\r\n", rendered)
            self.assertNotIn(b"\n", rendered.replace(b"\r\n", b""))

        private_config = json.loads(private_bytes.decode("utf-8"))
        self.assertEqual(private_config["username"], "Renée d'Italia")
        self.assertEqual(private_config["password"], 'pàss-"quoted"-\\slash-😀')
        self.assertEqual(private_config["custom_metadata"], "Caffè, città e l’apostrofo")

        hooks = json.loads(hooks_bytes.decode("utf-8"))
        self.assertEqual(hooks["metadata"]["owner"], "Renée")
        self.assertEqual(hooks["metadata"]["description"], 'Caffè "speciale" \\ 😀')
        self.assertEqual(
            hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
            'foreign-tool --label "città"',
        )
        self.assertEqual(len(hooks["hooks"]["Stop"]), 1)

        config = config_bytes.decode("utf-8")
        self.assertIn("Caffè, città, apostrofo l’Italia", config)
        self.assertIn('quote "ok", slash \\', config)
        self.assertIn("notify-ntfy.ps1", config)

        self._assert_private_acls(
            [
                self.home / "ntfy-config.json",
                self.home / "hooks.json",
                self.home / "config.toml",
                self.home / "ntfy-state",
            ]
        )
        self._assert_no_transaction_artifacts()

        stable_files = {
            name: (self.home / name).read_bytes()
            for name in ("ntfy-config.json", "hooks.json", "config.toml")
        }
        second = self._run_installer()
        self.assertEqual(second.returncode, 0, self._details(second))
        self.assertEqual(
            stable_files,
            {name: (self.home / name).read_bytes() for name in stable_files},
        )

    def test_invalid_utf8_or_unsafe_scalar_fails_before_any_mutation(self) -> None:
        cases = {
            "private-invalid-utf8": ("ntfy-config.json", b'{"topic":"bad\xff"}\n'),
            "hooks-invalid-utf8": ("hooks.json", b'{"hooks":{},"bad":"\xff"}\n'),
            "config-invalid-utf8": ("config.toml", b'# invalid \xff\n'),
            "notifier-invalid-utf8": ("notify-ntfy.ps1", b"# invalid \xff\r\n"),
            "watcher-invalid-utf8": ("watch-codex-ntfy.ps1", b"# invalid \xff\r\n"),
            "vbs-invalid-utf8": ("watch-codex-ntfy-hidden.vbs", b"' invalid \xff\r\n"),
            "replacement-scalar": (
                "hooks.json",
                self._encode_json({"hooks": {}, "unsafe": "replacement �"}),
            ),
            "private-singleton-array": (
                "ntfy-config.json",
                self._encode_json([{"topic": "must-not-scalarize"}]),
            ),
            "hooks-singleton-array": (
                "hooks.json",
                self._encode_json([{"hooks": {}}]),
            ),
        }
        for name, (relative_path, invalid_bytes) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(
                    prefix="codex ntfy remote invalid ",
                ) as case_directory:
                    case_home = Path(case_directory)
                    shutil.copy2(TARGET_INSTALLER, case_home / TARGET_INSTALLER.name)
                    original_home = self.home
                    self.home = case_home
                    try:
                        self._write_fixture()
                        (case_home / relative_path).write_bytes(invalid_bytes)
                        before = {
                            path.name: path.read_bytes()
                            for path in case_home.iterdir()
                            if path.is_file()
                        }
                        result = self._run_installer()
                        self.assertNotEqual(result.returncode, 0, self._details(result))
                        after = {
                            path.name: path.read_bytes()
                            for path in case_home.iterdir()
                            if path.is_file()
                        }
                        self.assertEqual(before, after)
                        self.assertFalse((case_home / "ntfy-state").exists())
                        self.assertFalse((case_home / "ntfy-backups").exists())
                        self._assert_no_transaction_artifacts()
                    finally:
                        self.home = original_home

    def test_all_semantic_preflight_finishes_before_backup_or_mutation(self) -> None:
        cases = ("config", "hooks", "task")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(
                    prefix="codex ntfy remote semantic ",
                ) as case_directory:
                    case_home = Path(case_directory)
                    shutil.copy2(TARGET_INSTALLER, case_home / TARGET_INSTALLER.name)
                    original_home = self.home
                    self.home = case_home
                    try:
                        self._write_fixture()
                        backup = case_home / "ntfy-backups" / "2099-01-01"
                        backup.mkdir(parents=True)
                        task_path = case_home / "simulated-task.txt"
                        task_path.write_text(
                            "managed=watch-codex-ntfy original\r\n",
                            encoding="utf-8",
                            newline="",
                        )
                        if case == "config":
                            (case_home / "config.toml").write_text(
                                'notify = ["foreign-tool"]\n',
                                encoding="utf-8",
                                newline="",
                            )
                        elif case == "hooks":
                            (case_home / "hooks.json").write_bytes(
                                self._encode_json({"hooks": {"Stop": {"bad": True}}})
                            )
                        else:
                            task_path.write_text(
                                "managed=foreign-tool\r\n",
                                encoding="utf-8",
                                newline="",
                            )
                        before = self._tree_snapshot()
                        result = self._run_installer(
                            skip_scheduled_task=case != "task",
                            environment={
                                "CODEX_NTFY_TEST_MODE": "1",
                                "CODEX_NTFY_TEST_REMOTE_TASK_PATH": str(task_path),
                            },
                        )
                        self.assertNotEqual(result.returncode, 0, self._details(result))
                        self.assertEqual(before, self._tree_snapshot())
                        self.assertFalse((backup / "hooks.json").exists())
                        self.assertFalse((case_home / "ntfy-state").exists())
                        self._assert_no_transaction_artifacts()
                    finally:
                        self.home = original_home

    def test_fault_after_each_phase_rolls_back_bytes_acls_task_and_state(self) -> None:
        phases = (
            "backup",
            "private-config",
            "codex-config",
            "hooks",
            "state",
            "doctor",
            "task-register",
            "task-start",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory(
                    prefix="codex ntfy remote rollback ",
                ) as case_directory:
                    case_home = Path(case_directory)
                    shutil.copy2(TARGET_INSTALLER, case_home / TARGET_INSTALLER.name)
                    original_home = self.home
                    self.home = case_home
                    try:
                        self._write_fixture(bom=True)
                        backup = case_home / "ntfy-backups" / "2099-01-01"
                        backup.mkdir(parents=True)
                        task_path = case_home / "simulated-task.txt"
                        task_path.write_text(
                            "managed=watch-codex-ntfy original\r\n",
                            encoding="utf-8",
                            newline="",
                        )
                        hostile_paths = [
                            case_home / "ntfy-config.json",
                            case_home / "hooks.json",
                            case_home / "config.toml",
                            task_path,
                        ]
                        self._add_hostile_explicit_acls(hostile_paths)
                        before = self._tree_snapshot()
                        result = self._run_installer(
                            skip_scheduled_task=False,
                            environment={
                                "CODEX_NTFY_TEST_MODE": "1",
                                "CODEX_NTFY_TEST_REMOTE_TASK_PATH": str(task_path),
                                "CODEX_NTFY_TEST_REMOTE_INSTALL_FAIL_AFTER": phase,
                            },
                        )
                        self.assertNotEqual(result.returncode, 0, self._details(result))
                        self.assertIn("Injected remote installer failure", self._details(result))
                        self.assertEqual(before, self._tree_snapshot(), self._details(result))
                        self._assert_no_transaction_artifacts()
                    finally:
                        self.home = original_home

    def test_simulated_task_transaction_is_idempotent(self) -> None:
        self._write_fixture()
        task_path = self.home / "simulated-task.txt"
        environment = {
            "CODEX_NTFY_TEST_MODE": "1",
            "CODEX_NTFY_TEST_REMOTE_TASK_PATH": str(task_path),
        }
        first = self._run_installer(
            skip_scheduled_task=False,
            environment=environment,
        )
        self.assertEqual(first.returncode, 0, self._details(first))
        self.assertIn(b"worker=Running", first.stdout)
        self.assertIn(b"managed=watch-codex-ntfy", task_path.read_bytes())
        stable = {
            path.name: path.read_bytes()
            for path in (
                self.home / "ntfy-config.json",
                self.home / "hooks.json",
                self.home / "config.toml",
                task_path,
            )
        }
        second = self._run_installer(
            skip_scheduled_task=False,
            environment=environment,
        )
        self.assertEqual(second.returncode, 0, self._details(second))
        self.assertEqual(stable, {name: (self.home / name).read_bytes() for name in stable})
        self._assert_no_transaction_artifacts()


if __name__ == "__main__":
    unittest.main()
