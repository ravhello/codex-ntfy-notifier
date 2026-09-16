"""Structural user-evidence parity using real parsers and temporary rollouts only."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = (
    Path(os.environ.get("WINDIR", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)
TURN = "00000000-0000-7000-8000-00000000e001"
OTHER_TURN = "00000000-0000-7000-8000-00000000e002"
SOURCE_THREAD = "00000000-0000-7000-8000-00000000e003"
METADATA = "internal_chat_message_metadata_passthrough"


def lifecycle(kind: str, turn: str = TURN) -> dict:
    payload = {"type": kind, "turn_id": turn}
    if kind == "task_complete":
        payload["last_agent_message"] = "Finished the requested work."
    elif kind == "user_message":
        payload["message"] = "Legacy user request."
    return {"type": "event_msg", "payload": payload}


def modern_message(turn: str = TURN) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "A real user request."}],
            METADATA: {"turn_id": turn, "content_item_kinds": ["user.text"]},
        },
    }


def host_delegation(turn: str = TURN) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "namespace": "codex_app",
            "name": "send_message_to_thread",
            "output": (
                "<codex_delegation>\n"
                f"  <source_thread_id>{SOURCE_THREAD}</source_thread_id>\n"
                "  <input>A requested follow-up in this task.</input>\n"
                "</codex_delegation>"
            ),
            METADATA: {"turn_id": turn},
        },
    }


def evidence_cases() -> list[dict]:
    cases = []

    def add(name, item=None, expected=False, *, lines=None, candidate=TURN):
        if lines is None:
            lines = [lifecycle("task_started"), item, lifecycle("task_complete")]
        cases.append({
            "name": name,
            "candidate": candidate,
            "expected": expected,
            "lines": [json.dumps(line, ensure_ascii=False) for line in lines],
        })

    def changed(path, value):
        item = modern_message()
        target = item
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return item

    add("legacy_user_message", lifecycle("user_message"), True)
    add("modern_user_text", modern_message(), True)
    mixed = modern_message()
    mixed["payload"]["content"].insert(0, {"type": "input_text", "text": "Injected environment"})
    mixed["payload"][METADATA]["content_item_kinds"].insert(0, "environment_context")
    add("explicit_user_text_after_environment", mixed, True)
    multiple = modern_message()
    multiple["payload"]["content"].append({"type": "input_text", "text": "A second part."})
    multiple["payload"][METADATA]["content_item_kinds"].append("user.text")
    add("multiple_explicit_user_text_parts", multiple, True)

    add("wrong_turn", modern_message(OTHER_TURN))
    for role in ("assistant", "tool", "USER", None, 7, ["user"], {"role": "user"}):
        add("role_" + repr(role), changed(["payload", "role"], role))
    for kind in ("MESSAGE", None, 7, ["message"], {"type": "message"}):
        add("message_type_" + repr(kind), changed(["payload", "type"], kind))
    for metadata_kind in ("environment_context", "unknown", "USER.TEXT"):
        add("non_user_kind_" + metadata_kind,
            changed(["payload", METADATA, "content_item_kinds"], [metadata_kind]))

    missing = modern_message()
    del missing["payload"][METADATA]
    add("missing_metadata", missing)
    for value in (None, "metadata", 7, [modern_message()["payload"][METADATA]]):
        add("malformed_metadata_" + repr(value), changed(["payload", METADATA], value))
    for value in (None, 7, [TURN], {"turn_id": TURN}, " " + TURN):
        add("malformed_turn_" + repr(value), changed(["payload", METADATA, "turn_id"], value))
    missing_turn = modern_message()
    del missing_turn["payload"][METADATA]["turn_id"]
    add("missing_metadata_turn", missing_turn)
    missing_kinds = modern_message()
    del missing_kinds["payload"][METADATA]["content_item_kinds"]
    add("missing_content_kinds", missing_kinds)
    for value in (None, "user.text", [], ["user.text", "user.text"], [None], [7], [["user.text"]]):
        add("malformed_kinds_" + repr(value),
            changed(["payload", METADATA, "content_item_kinds"], value))
    for value in (None, "text", {}, [], {"type": "input_text", "text": "Request"}, ["Request"], [None]):
        add("malformed_content_" + repr(value), changed(["payload", "content"], value))
    for value in (None, 7, ["input_text"], "output_text", "INPUT_TEXT"):
        add("malformed_item_type_" + repr(value), changed(["payload", "content", 0, "type"], value))
    for value in (None, 7, ["Request"], {"text": "Request"}, "", " \t\r\n"):
        add("malformed_or_blank_text_" + repr(value), changed(["payload", "content", 0, "text"], value))
    for field in ("type", "text"):
        missing_item_field = modern_message()
        del missing_item_field["payload"]["content"][0][field]
        add("missing_item_" + field, missing_item_field)

    # A valid part cannot mask a structurally malformed sibling.
    for sibling, kind in ((None, "environment_context"),
                          ({"type": 7, "text": "Environment"}, "environment_context"),
                          ({"type": "input_text", "text": "Environment"}, None),
                          ({"type": "input_text", "text": " "}, "user.text")):
        malformed_sibling = modern_message()
        malformed_sibling["payload"]["content"].append(sibling)
        malformed_sibling["payload"][METADATA]["content_item_kinds"].append(kind)
        add("malformed_sibling_" + repr((sibling, kind)), malformed_sibling)

    add("array_payload", {"type": "response_item", "payload": [modern_message()["payload"]]})
    add("array_envelope_type", {"type": ["response_item"], "payload": modern_message()["payload"]})
    add("wrong_envelope_type", {"type": "other", "payload": modern_message()["payload"]})
    add("before_start", lines=[modern_message(), lifecycle("task_started"), lifecycle("task_complete")])
    add("after_completion", lines=[lifecycle("task_started"), lifecycle("task_complete"), modern_message()])
    add("after_abort", lines=[lifecycle("task_started"), lifecycle("turn_aborted"), modern_message()])
    add("inherited_message_wrong_open_turn", lines=[
        lifecycle("task_started", OTHER_TURN), modern_message(), lifecycle("task_complete", OTHER_TURN),
        lifecycle("task_started"), lifecycle("task_complete"),
    ])
    add("previous_turn_evidence_does_not_contaminate_candidate", lines=[
        lifecycle("task_started", OTHER_TURN), modern_message(OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
        lifecycle("task_started"), lifecycle("task_complete"),
    ])
    add("later_turn_evidence_does_not_contaminate_candidate", lines=[
        lifecycle("task_started"), lifecycle("task_complete"),
        lifecycle("task_started", OTHER_TURN), modern_message(OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
    ])
    add("metadata_for_open_but_noncurrent_turn", lines=[
        lifecycle("task_started"), lifecycle("task_started", OTHER_TURN), modern_message(),
        lifecycle("task_complete", OTHER_TURN), lifecycle("task_complete"),
    ])
    add("candidate_evidence_survives_later_turn", expected=True, lines=[
        lifecycle("task_started"), modern_message(), lifecycle("task_complete"),
        lifecycle("task_started", OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
    ])

    # The host inbox is not an ordinary model tool result: it has exact host
    # provenance, a complete delegation envelope, and no call_id key at all.
    def changed_host(path, value):
        item = host_delegation()
        target = item
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return item

    add("host_delegation", host_delegation(), True)
    add("host_delegation_wrapper_whitespace",
        changed_host(["payload", "output"], " \n" + host_delegation()["payload"]["output"] + "\n\t"), True)
    add("host_delegation_wrong_turn", host_delegation(OTHER_TURN))
    for namespace in ("functions", "mcp__codex_app", "CODEX_APP", "", None, 7, ["codex_app"]):
        add("host_namespace_" + repr(namespace), changed_host(["payload", "namespace"], namespace))
    for name in ("automation_update", "read_thread", "SEND_MESSAGE_TO_THREAD", "", None, 7,
                 ["send_message_to_thread"]):
        add("host_name_" + repr(name), changed_host(["payload", "name"], name))
    for call_id in ("call_ordinary_tool_return", "", None, 0, []):
        add("host_call_id_present_" + repr(call_id), changed_host(["payload", "call_id"], call_id))
    for kind in ("message", "function_call", "FUNCTION_CALL_OUTPUT", None, ["function_call_output"]):
        add("host_payload_type_" + repr(kind), changed_host(["payload", "type"], kind))
    for output in (None, 7, [], {"output": host_delegation()["payload"]["output"]},
                   [host_delegation()["payload"]["output"]], "", " \t\n", "ordinary tool output"):
        add("host_non_wrapper_output_" + repr(output), changed_host(["payload", "output"], output))
    wrapper = host_delegation()["payload"]["output"]
    malformed_wrappers = {
        "missing_close": wrapper.replace("</codex_delegation>", ""),
        "wrong_close": wrapper.replace("</codex_delegation>", "</delegation>"),
        "wrong_input_tag": wrapper.replace("<input>", "<instructions>").replace("</input>", "</instructions>"),
        "prefixed_tool_output": "Tool returned: " + wrapper,
        "suffixed_tool_output": wrapper + " trailing tool output",
        "no_source": wrapper.replace(f"<source_thread_id>{SOURCE_THREAD}</source_thread_id>", ""),
        "invalid_source": wrapper.replace(SOURCE_THREAD, "not-a-uuid"),
        "uuid_without_hyphens": wrapper.replace(SOURCE_THREAD, SOURCE_THREAD.replace("-", "")),
        "uuid_braces": wrapper.replace(SOURCE_THREAD, "{" + SOURCE_THREAD + "}"),
        "uuid_nonhex": wrapper.replace(SOURCE_THREAD, "00000000-0000-7000-8000-00000000zzzz"),
        "empty_input": wrapper.replace("A requested follow-up in this task.", ""),
        "blank_input": wrapper.replace("A requested follow-up in this task.", " \t\n"),
    }
    for name, output in malformed_wrappers.items():
        add("host_malformed_wrapper_" + name, changed_host(["payload", "output"], output))
    for metadata in (None, "metadata", 7, [host_delegation()["payload"][METADATA]], {}):
        add("host_malformed_metadata_" + repr(metadata), changed_host(["payload", METADATA], metadata))
    for turn in (None, 7, [TURN], {"turn_id": TURN}, " " + TURN):
        add("host_malformed_turn_" + repr(turn), changed_host(["payload", METADATA, "turn_id"], turn))
    for field in ("namespace", "name", "output", METADATA):
        missing_host_field = host_delegation()
        del missing_host_field["payload"][field]
        add("host_missing_" + field, missing_host_field)
    add("host_array_payload", {"type": "response_item", "payload": [host_delegation()["payload"]]})
    add("host_array_envelope_type", {"type": ["response_item"], "payload": host_delegation()["payload"]})
    add("host_wrong_envelope_type", {"type": "other", "payload": host_delegation()["payload"]})
    add("host_before_start", lines=[host_delegation(), lifecycle("task_started"), lifecycle("task_complete")])
    add("host_after_completion", lines=[lifecycle("task_started"), lifecycle("task_complete"), host_delegation()])
    add("host_after_abort", lines=[lifecycle("task_started"), lifecycle("turn_aborted"), host_delegation()])
    add("host_open_but_noncurrent_turn", lines=[
        lifecycle("task_started"), lifecycle("task_started", OTHER_TURN), host_delegation(),
        lifecycle("task_complete", OTHER_TURN), lifecycle("task_complete"),
    ])
    add("host_previous_turn_does_not_contaminate_candidate", lines=[
        lifecycle("task_started", OTHER_TURN), host_delegation(OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
        lifecycle("task_started"), lifecycle("task_complete"),
    ])
    add("host_later_turn_does_not_contaminate_candidate", lines=[
        lifecycle("task_started"), lifecycle("task_complete"),
        lifecycle("task_started", OTHER_TURN), host_delegation(OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
    ])
    add("host_evidence_survives_later_turn", expected=True, lines=[
        lifecycle("task_started"), host_delegation(), lifecycle("task_complete"),
        lifecycle("task_started", OTHER_TURN), lifecycle("task_complete", OTHER_TURN),
    ])
    return cases


POWERSHELL_HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$Utf8StrictNoBom = [Text.UTF8Encoding]::new($false, $true)
$MaxFallbackRolloutLineBytes = 8 * 1024 * 1024
$script:RolloutProbeCache = @{}
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  $env:MODERN_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
# Load definitions only: never execute the notifier entry point or create workers.
foreach ($definition in $ast.EndBlock.Statements) {
  if ($definition -is [Management.Automation.Language.FunctionDefinitionAst]) {
    . ([scriptblock]::Create($definition.Extent.Text))
  }
}
Initialize-WinSqlite
$cases = [IO.File]::ReadAllText($env:MODERN_CASES) | ConvertFrom-Json
$results = @()
$index = 0
foreach ($case in $cases) {
  $path = Join-Path $env:MODERN_TEMP ("powershell-" + $index + '.jsonl')
  [IO.File]::WriteAllText($path, '', $Utf8StrictNoBom)
  $incrementalOk = $true
  $incrementalErrors = @()
  foreach ($line in $case.lines) {
    [IO.File]::AppendAllText($path, ([string]$line + "`n"), $Utf8StrictNoBom)
    $probe = Update-RolloutProbe -Path $path -IncludeMessage $false
    if (-not $probe.ok) {
      $incrementalOk = $false
      $incrementalErrors += [string]$probe.error
    }
  }
  $summary = [CodexNtfyWinSqlite]::ScanLifecycleSummary($path, [string]$case.candidate, $false)
  $results += [pscustomobject]@{
    name = [string]$case.name
    cold_has_user = $summary[2] -eq '1'
    cold_summary_count = $summary.Count
    incremental_has_user = $probe.state.userMessageTurns.ContainsKey([string]$case.candidate)
    incremental_ok = $incrementalOk
    incremental_errors = @($incrementalErrors)
  }
  $index++
}
ConvertTo-Json -InputObject @($results) -Depth 5 -Compress
"""


class ModernUserEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "modern_user_evidence_notifier", ROOT / "src" / "notify-ntfy.py"
        )
        assert spec is not None and spec.loader is not None
        cls.notifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.notifier)
        cls.cases = evidence_cases()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ntfy-modern-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        network_guard = mock.patch.object(
            self.notifier.urllib.request, "urlopen", side_effect=AssertionError("unexpected HTTP")
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def assert_python_matrix(self, incremental: bool) -> None:
        for index, case in enumerate(self.cases):
            with self.subTest(case=case["name"], incremental=incremental):
                path = self.directory / f"python-{index}.jsonl"
                record = {"thread_id": "isolated-parser-thread", "turn_id": case["candidate"]}
                path.write_text("", encoding="utf-8")
                with mock.patch.object(self.notifier, "find_rollout", return_value=path):
                    if incremental:
                        for line in case["lines"]:
                            with path.open("a", encoding="utf-8", newline="\n") as handle:
                                handle.write(line + "\n")
                            probe = self.notifier.update_idle_probe(None, record, False)
                    else:
                        path.write_text("\n".join(case["lines"]) + "\n", encoding="utf-8")
                        probe = self.notifier.update_idle_probe(None, record, False)
                self.assertEqual(probe["candidate_user_message"], case["expected"])
                self.assertFalse(probe["invalid_lifecycle"], probe)
                self.assertFalse(probe["incomplete_tail"], probe)

    def test_python_cold_probe_structural_matrix(self) -> None:
        self.assert_python_matrix(incremental=False)

    def test_python_incremental_probe_structural_matrix(self) -> None:
        self.assert_python_matrix(incremental=True)

    @unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows parser parity")
    def test_powershell_cold_csharp_and_incremental_structural_matrix(self) -> None:
        fixture = self.directory / "cases.json"
        fixture.write_text(json.dumps(self.cases, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", POWERSHELL_HARNESS],
            env={
                **os.environ,
                "MODERN_NOTIFIER": str(ROOT / "src" / "notify-ntfy.ps1"),
                "MODERN_CASES": str(fixture),
                "MODERN_TEMP": str(self.directory),
            },
            capture_output=True,
            encoding="utf-8",
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        results = json.loads(result.stdout)
        self.assertEqual(len(results), len(self.cases))
        for case, observed in zip(self.cases, results):
            with self.subTest(case=case["name"]):
                self.assertEqual(observed["name"], case["name"])
                self.assertEqual(observed["cold_summary_count"], 23, observed)
                self.assertTrue(observed["incremental_ok"], observed)
                self.assertEqual(observed["cold_has_user"], case["expected"], observed)
                self.assertEqual(observed["incremental_has_user"], case["expected"], observed)


if __name__ == "__main__":
    unittest.main()
