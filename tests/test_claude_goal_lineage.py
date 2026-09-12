from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def node(identity: str, parent: str | None, *, goal: bool | None = None, kind: str = "assistant") -> dict:
    value = {"type": "attachment" if goal is not None else kind, "uuid": identity, "parentUuid": parent}
    if goal is not None:
        value["attachment"] = {"type": "goal_status", "met": goal}
    return value


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows PowerShell lineage test")
class ClaudeGoalLineageTests(unittest.TestCase):
    """Import pure readers only: no worker, hooks, requests, or real user state."""

    def probe(self, records: list[dict | str], *, limit: int = 0, prior_active: bool = False,
              matching_epoch: bool = True) -> dict:
        with tempfile.TemporaryDirectory(prefix="ntfy-goal-lineage-") as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("\n".join(value if isinstance(value, str) else json.dumps(value)
                                            for value in records) + "\n", encoding="utf-8")
            script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SOURCE, [ref]$null, [ref]$null)
foreach ($statement in $ast.EndBlock.Statements) {
  if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
    . ([scriptblock]::Create($statement.Extent.Text))
  }
}
function Invoke-WebRequest { throw 'HTTP prohibited in reader test' }
function Invoke-RestMethod { throw 'HTTP prohibited in reader test' }
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$ClaudeGoalMaxLineBytes = 1024 * 1024
$script:ClaudeGoalStateCache = @{}
$goal = Get-ClaudeGoalTranscriptState -TranscriptPath $env:TEST_TRANSCRIPT -MaxBytes ([int64]$env:TEST_LIMIT)
$gate = $null
if ($env:TEST_PRIOR_ACTIVE -eq '1') {
  function Read-ClaudeSessionState {
    param([string]$SessionId)
    return [pscustomobject]@{ epoch = [int64]$env:TEST_EPOCH; prompt_id = 'current-prompt'; transcript_path = $env:TEST_TRANSCRIPT }
  }
  $record = [pscustomobject]@{
    provider = 'claude'; candidate_kind = 'claude_stop'; thread_id = 'session'; turn_id = 'current-prompt'
    claude_session_epoch = 7; claude_goal_state = 'active'; claude_goal_marker = 'abandoned-goal'
    candidate_rollout_path = $env:TEST_TRANSCRIPT; created_unix_ms = 1
  }
  $gate = Test-RecordIdleGate -Record $record -Config ([pscustomobject]@{ idleGraceSeconds = 0 })
}
[pscustomobject]@{ goal = $goal; gate = $gate } | ConvertTo-Json -Depth 5 -Compress
"""
            env = os.environ.copy()
            env.update(TEST_SOURCE=str(ROOT / "src/notify-ntfy.ps1"), TEST_TRANSCRIPT=str(transcript),
                       TEST_LIMIT=str(limit), TEST_PRIOR_ACTIVE="1" if prior_active else "0",
                       TEST_EPOCH="7" if matching_epoch else "8", CODEX_NTFY_NO_SPAWN="1")
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                     base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                                    timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout.strip())

    @staticmethod
    def branched() -> list[dict]:
        return [node("root", None, kind="user"), node("abandoned-goal", "root", goal=False),
                node("old-final", "abandoned-goal"), node("current-user", "root", kind="user"),
                node("current-final", "current-user"),
                {**node("stop-receipt", "current-final", kind="attachment"),
                 "attachment": {"type": "hook_success", "hookEvent": "Stop", "exitCode": 0}},
                {"type": "last-prompt", "leafUuid": "current-final", "sessionId": "session"}]

    def test_goal_on_abandoned_branch_does_not_block_current_stop(self) -> None:
        result = self.probe(self.branched(), prior_active=True)
        self.assertEqual(result["goal"]["state"], "none")
        self.assertTrue(result["goal"]["lineage_verified"])
        self.assertEqual(result["gate"]["state"], "ready")

    def test_active_goal_inherited_on_same_branch_stays_active(self) -> None:
        result = self.probe([node("root", None, kind="user"), node("goal", "root", goal=False),
                             node("resumed-prompt", "goal", kind="user"), node("final", "resumed-prompt")])
        self.assertEqual(result["goal"]["state"], "active")
        self.assertEqual(result["goal"]["marker"], "goal")

    def test_achieved_goal_on_current_branch_releases(self) -> None:
        result = self.probe([node("root", None), node("active", "root", goal=False),
                             node("achieved", "active", goal=True), node("final", "achieved")], prior_active=True)
        self.assertEqual(result["goal"]["state"], "achieved")
        self.assertEqual(result["gate"]["state"], "ready")

    def test_missing_malformed_or_cyclic_parent_fails_closed(self) -> None:
        cases = [
            [node("final", "missing")],
            [node("root", None), {**node("final", "root"), "parentUuid": 123}],
            [node("root", None), {"type": "assistant", "uuid": "final"}],
            [node("a", "b"), node("b", "a")],
            [node("root", None), '{"type":"assistant","uuid":"final","parentUuid":'],
        ]
        for records in cases:
            with self.subTest(records=records):
                self.assertEqual(self.probe(records)["goal"]["state"], "unknown")

    def test_bounded_scan_with_unresolved_ancestor_fails_closed(self) -> None:
        records = [node("root", None), {"type": "progress", "payload": "x" * 1000}, node("final", "root")]
        self.assertEqual(self.probe(records, limit=200)["goal"]["state"], "unknown")

    def test_malformed_sidechain_flag_cannot_hide_active_goal(self) -> None:
        for malformed in ("false", "true", 0, None):
            with self.subTest(isSidechain=malformed):
                records = [node("root", None), {**node("goal", "root", goal=False), "isSidechain": malformed}]
                self.assertEqual(self.probe(records)["goal"]["state"], "unknown")

    def test_prior_active_candidate_cannot_reconcile_against_new_prompt_epoch(self) -> None:
        result = self.probe(self.branched(), prior_active=True, matching_epoch=False)
        self.assertEqual(result["gate"]["state"], "busy")

    def test_newer_explicit_leaf_pointer_selects_active_or_inactive_branch(self) -> None:
        for target, expected in (("active-final", "active"), ("inactive-final", "none")):
            with self.subTest(target=target):
                records = [node("root", None), node("inactive-final", "root"),
                           node("goal", "root", goal=False), node("active-final", "goal"),
                           {"type": "last-prompt", "leafUuid": target, "explicit": True}]
                self.assertEqual(self.probe(records)["goal"]["state"], expected)

    def test_old_explicit_pointer_does_not_override_newer_current_stop(self) -> None:
        records = [node("root", None), node("goal", "root", goal=False), node("old-final", "goal"),
                   {"type": "last-prompt", "leafUuid": "old-final", "explicit": True},
                   node("current-final", "root"),
                   {**node("stop-receipt", "current-final", kind="attachment"),
                    "attachment": {"type": "hook_success", "hookEvent": "Stop"}}]
        self.assertEqual(self.probe(records)["goal"]["state"], "none")

    def test_unresolved_or_malformed_explicit_pointer_fails_closed(self) -> None:
        for target in ("missing-leaf", "", None, 42):
            with self.subTest(target=target):
                records = [node("root", None), {"type": "last-prompt", "leafUuid": target, "explicit": True}]
                self.assertEqual(self.probe(records)["goal"]["state"], "unknown")

    def test_legacy_unlinked_goal_markers_remain_supported(self) -> None:
        result = self.probe([{"type": "attachment", "uuid": "legacy", "attachment": {"type": "goal_status", "met": True}}])
        self.assertEqual(result["goal"]["state"], "achieved")

    def test_large_irrelevant_record_keeps_bounded_legacy_scan_compatible(self) -> None:
        result = self.probe([{"type": "attachment", "uuid": "legacy", "attachment": {"type": "goal_status", "met": False}},
                             {"type": "progress", "payload": "x" * (2 * 1024 * 1024)}])
        self.assertEqual(result["goal"]["state"], "active")

    def test_oversize_linked_record_is_not_skipped_as_irrelevant(self) -> None:
        result = self.probe([node("root", None), {**node("final", "root"), "payload": "x" * (2 * 1024 * 1024)}])
        self.assertEqual(result["goal"]["state"], "unverifiable")


if __name__ == "__main__":
    unittest.main()
