"""Question attention contracts: typed histories and loopback HTTP only."""
from __future__ import annotations

import base64
from contextlib import closing
import copy
import hashlib
import importlib.util
from itertools import islice
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import test_notifiers as contracts
from notifier_test_io import read_text_shared_delete


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = contracts.WINDOWS_POWERSHELL
HAS_PS = os.name == "nt" and POWERSHELL.exists()
SECRET_QUESTION = "PRIVATE_QUESTION_DO_NOT_STORE_é_汉字"
SECRET_ANSWER = "PRIVATE_ANSWER_DO_NOT_STORE"


def load_notifier():
    spec = importlib.util.spec_from_file_location("attention_contract_notifier", ROOT / "src/notify-ntfy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(kind: str, turn: str, **values) -> dict:
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **values}}


def codex_call(turn: str, call: str, *, name="request_user_input_async", count=1) -> dict:
    questions = ([{"title": SECRET_QUESTION, "options": ["A", "B"]} for _ in range(count)]
                 if name.endswith("_async") else
                 [{"id": "choice", "header": "Choice", "question": SECRET_QUESTION,
                   "options": [{"label": "A", "description": "First"},
                               {"label": "B", "description": "Second"}]}])
    return {"type": "response_item", "payload": {
        "type": "function_call", "name": name, "status": "completed", "call_id": call,
        "arguments": json.dumps({"questions": questions}),
        "internal_chat_message_metadata_passthrough": {"turn_id": turn, "create_time": 0},
    }}


def codex_output(turn: str, call: str, output: object = None) -> dict:
    return {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call,
        "output": json.dumps({"accepted": True} if output is None else output),
        "internal_chat_message_metadata_passthrough": {"turn_id": turn},
    }}


def codex_user(turn: str, text: str) -> dict:
    return {"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"turn_id": turn, "content_item_kinds": ["user.text"]},
    }}


def codex_reply(turn: str, call: str, indices=(0,), *, single=False, answer=SECRET_ANSWER) -> dict:
    answers = [{"questionItemId": json.dumps(["request_user_input_async", call, index], separators=(",", ":")),
                "question": SECRET_QUESTION, "answer": answer} for index in indices]
    value = answers[0] if single else answers
    return codex_user(turn, "<send_user_message_question_reply>" + json.dumps(value) +
                      "</send_user_message_question_reply>")


def write_history(path: Path, records: list[dict | str], *, raw_suffix=b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                     for value in records) + "\n"
    path.write_bytes(data.encode("utf-8") + raw_suffix)


def append_history(path: Path, records: list[dict]) -> None:
    with path.open("ab") as stream:
        stream.write(("\n".join(json.dumps(value, ensure_ascii=False) for value in records) + "\n").encode("utf-8"))


PS_PROBE = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:ATTENTION_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'source parse failed'}
foreach($statement in $ast.EndBlock.Statements){
  if($statement -is [Management.Automation.Language.FunctionDefinitionAst]){
    . ([scriptblock]::Create($statement.Extent.Text))
  }
}
$Utf8NoBom=New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom=New-Object Text.UTF8Encoding($false,$true)
$script:AttentionProbeCache=@{}
$script:ClaudeGoalStateCache=@{}
$ClaudeGoalMaxLineBytes=1024*1024
function Write-RuntimeLog { param($Message) throw ('unexpected reader log: '+$Message) }
function Send-NtfyEvent { throw 'HTTP prohibited in reader test' }
function Invoke-RestMethod { throw 'HTTP prohibited in reader test' }
function Invoke-WebRequest { throw 'HTTP prohibited in reader test' }
$request=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:ATTENTION_REQUEST))|ConvertFrom-Json
$arguments=@{TranscriptPath=[string]$request.path;ThreadId=[string]$request.thread}
if($null -ne $request.expected){
  $arguments.ExpectedTurnId=[string]$request.expected.turn_id
  $arguments.ExpectedCallId=[string]$request.expected.call_id
}
if($request.provider -eq 'claude'){$result=Get-ClaudeAttentionState @arguments}
else{$result=Get-CodexAttentionState @arguments}
ConvertTo-Json -InputObject $result -Depth 12 -Compress
"""


class AttentionProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.notifier = load_notifier()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ntfy-attention-probe-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.thread, self.turn = str(uuid.uuid4()), str(uuid.uuid4())
        self.call = "call_attention_" + uuid.uuid4().hex
        self.path = self.home / f"{self.thread}.jsonl"
        network_guard = mock.patch.object(self.notifier.urllib.request, "urlopen",
                                          side_effect=AssertionError("unexpected HTTP in pure probe"))
        network_guard.start()
        self.addCleanup(network_guard.stop)
        self.prefix = [{"type": "session_meta", "payload": {"id": self.thread, "source": "vscode"}},
                       event("task_started", self.turn), event("user_message", self.turn, message="Choose how to proceed")]

    def ps_probe(self, *, provider="codex", expected=None) -> dict:
        request = {"provider": provider, "path": str(self.path), "thread": self.thread, "expected": expected}
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(PS_PROBE.encode("utf-16-le")).decode("ascii")],
            env={**os.environ, "ATTENTION_SOURCE": str(ROOT / "src/notify-ntfy.ps1"),
                 "ATTENTION_REQUEST": base64.b64encode(json.dumps(request).encode()).decode(),
                 "CODEX_NTFY_NO_SPAWN": "1"}, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=45,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    def probes(self, records: list[dict | str], *, expected=None, suffix=b"") -> list[dict]:
        write_history(self.path, records, raw_suffix=suffix)
        return self.probe_existing(expected=expected)

    def probe_existing(self, *, expected=None) -> list[dict]:
        results = [self.notifier.codex_attention_state(self.path, self.thread, expected=expected)]
        if HAS_PS:
            results.append(self.ps_probe(expected=expected))
        for result in results:
            self.assertNotIn(SECRET_QUESTION, json.dumps(result, ensure_ascii=False))
            self.assertNotIn(SECRET_ANSWER, json.dumps(result, ensure_ascii=False))
        if len(results) == 2:
            fields = ["state", "paused_turns"]
            if results[0]["state"] in ("waiting", "resolved"):
                fields += ["turn_id", "call_id", "tool_name", "pending_count"]
            for field in fields:
                self.assertEqual(results[0].get(field), results[1].get(field), (field, results))
        return results

    def assert_state(self, results: list[dict], state: str, *, remaining=None) -> None:
        for result in results:
            self.assertEqual(result["state"], state, result)
            if remaining is not None:
                self.assertEqual(result["pending_count"], remaining, result)

    def waiting(self, *, count=1) -> list[dict]:
        return self.prefix + [codex_call(self.turn, self.call, count=count), codex_output(self.turn, self.call)]

    def test_async_acceptance_is_not_an_answer_and_completed_turn_is_a_pause(self) -> None:
        records = self.waiting() + [event("task_complete", self.turn, last_agent_message="Final asks a question")]
        results = self.probes(records)
        self.assert_state(results, "waiting", remaining=1)
        for result in results:
            self.assertEqual(result["call_id"], self.call)
            self.assertEqual(result["turn_id"], self.turn)
            self.assertIn(self.turn, result["paused_turns"])
        qualified = self.probes(self.prefix + [codex_call(self.turn, self.call,
                                name="functions.request_user_input_async"), codex_output(self.turn, self.call)])
        self.assert_state(qualified, "waiting", remaining=1)
        for result in qualified:
            self.assertEqual(result["tool_name"], "request_user_input_async")

    def test_async_requires_positive_acceptance(self) -> None:
        for output in (False, {"accepted": False}, {"accepted": "true"}, {"error": "not displayed"}):
            with self.subTest(output=output):
                results = self.probes(self.prefix + [codex_call(self.turn, self.call),
                                                    codex_output(self.turn, self.call, output)])
                for result in results:
                    self.assertNotEqual(result["state"], "waiting", result)
        for result in self.probes(self.prefix + [codex_call(self.turn, self.call)]):
            self.assertNotEqual(result["state"], "waiting", result)

    def test_single_array_partial_and_empty_replies_are_bound_by_exact_index(self) -> None:
        for single, answer in ((True, SECRET_ANSWER), (False, "")):
            with self.subTest(single=single, empty=not answer):
                results = self.probes(self.waiting(count=2) +
                                      [codex_reply(self.turn, self.call, (0,), single=single, answer=answer)])
                self.assert_state(results, "waiting", remaining=1)
                results = self.probes(self.waiting(count=2) +
                                      [codex_reply(self.turn, self.call, (0,), single=single, answer=answer),
                                       codex_reply(self.turn, self.call, (1,), single=True, answer="")])
                self.assert_state(results, "resolved", remaining=0)
        other = "call_partial_second_" + uuid.uuid4().hex
        records = self.waiting() + [codex_call(self.turn, other), codex_output(self.turn, other),
                                   codex_reply(self.turn, other, single=True),
                                   event("task_complete", self.turn)]
        results = self.probes(records)
        self.assert_state(results, "waiting", remaining=1)
        for result in results:
            self.assertEqual(result["call_id"], self.call,
                             "answering a later call cannot hide an earlier unanswered call")
            self.assertIn(self.turn, result["paused_turns"])
        self.assert_state(self.probe_existing(expected={"turn_id": self.turn, "call_id": self.call}),
                          "waiting", remaining=1)
        results = self.probes(records + [codex_reply(self.turn, self.call, single=True),
                                        event("task_complete", self.turn)])
        self.assert_state(results, "resolved", remaining=0)
        for result in results:
            self.assertNotIn(self.turn, result["paused_turns"])
            self.assertIn(self.turn, result.get("resumed_turns", []))

    def test_wrong_call_index_and_quoted_reply_do_not_answer_current_question(self) -> None:
        for reply in (codex_reply(self.turn, "call_wrong"), codex_reply(self.turn, self.call, (99,)),
                      {"type": "response_item", "payload": {
                          "type": "message", "role": "assistant", "content": [{"type": "output_text",
                          "text": codex_reply(self.turn, self.call)["payload"]["content"][0]["text"]}]}}):
            with self.subTest(reply_kind=str(reply)[0:40]):
                self.assert_state(self.probes(self.waiting() + [reply]), "waiting", remaining=1)

    def test_reply_does_not_release_old_pause_completion_in_next_start_gap(self) -> None:
        records = self.waiting() + [event("task_complete", self.turn),
                                   codex_reply(self.turn, self.call, single=True)]
        results = self.probes(records)
        self.assert_state(results, "resolved", remaining=0)
        for result in results:
            self.assertIn(self.turn, result["paused_turns"])
            self.assertNotIn(self.turn, result.get("resumed_turns", []))
            self.assertEqual(self.notifier.attention_completion_disposition({"turn_id": self.turn}, result),
                             "attention-pause")
        results = self.probes(records + [event("task_complete", self.turn, last_agent_message="After answer")])
        for result in results:
            self.assertNotIn(self.turn, result["paused_turns"])
            self.assertIn(self.turn, result.get("resumed_turns", []),
                          "revival needs positive post-answer terminal proof, not mere absence")

    def test_new_foreground_input_cancels_old_call_but_lifecycle_alone_does_not(self) -> None:
        next_turn = str(uuid.uuid4())
        self.assert_state(self.probes(self.waiting() + [event("task_started", next_turn),
                                                      {"type": "turn_context", "payload": {"turn_id": next_turn}}]),
                          "waiting", remaining=1)
        self.assert_state(self.probes(self.waiting() + [codex_user(next_turn, "Use my new request instead")]),
                          "resolved", remaining=0)
        self.assert_state(self.probes(self.waiting() + [event("turn_aborted", self.turn)]), "resolved", remaining=0)

    def test_blocking_call_requires_no_ack_and_matching_output_resolves(self) -> None:
        records = self.prefix + [codex_call(self.turn, self.call, name="request_user_input")]
        self.assert_state(self.probes(records), "waiting", remaining=1)
        self.assert_state(self.probes(records + [codex_output(self.turn, self.call, {"answers": {}})]),
                          "resolved", remaining=0)
        qualified = self.probes(self.prefix + [codex_call(self.turn, self.call,
                                                         name="functions.request_user_input")])
        self.assert_state(qualified, "waiting", remaining=1)
        for result in qualified:
            self.assertEqual(result["tool_name"], "request_user_input")

    def test_duplicate_history_is_idempotent_and_new_call_gets_new_identity(self) -> None:
        records = self.waiting()
        self.assert_state(self.probes(records + copy.deepcopy(records[-2:])), "waiting", remaining=1)
        results = self.probes(records + [codex_reply(self.turn, self.call), *copy.deepcopy(records[-2:])])
        self.assert_state(results, "resolved", remaining=0)
        other = "call_next_" + uuid.uuid4().hex
        results = self.probes(records + [codex_reply(self.turn, self.call), codex_call(self.turn, other),
                                        codex_output(self.turn, other)])
        self.assert_state(results, "waiting", remaining=1)
        for result in results:
            self.assertEqual(result["call_id"], other)

    def test_conflicting_duplicate_and_untrusted_tool_namespace_cannot_create_attention(self) -> None:
        records = self.waiting()
        conflict = copy.deepcopy(records[-2])
        conflict["payload"]["arguments"] = json.dumps({"questions": [{"title": "Conflicting call"}]})
        for result in self.probes(records + [conflict]):
            self.assertEqual(result["state"], "unknown", result)
        wrong_namespace = self.waiting()
        wrong_namespace[-2]["payload"]["namespace"] = "untrusted_mcp"
        for result in self.probes(wrong_namespace):
            self.assertNotEqual(result["state"], "waiting", result)

    def test_missing_expected_question_evidence_is_not_proven_resolved(self) -> None:
        for result in self.probes(self.prefix, expected={"turn_id": self.turn, "call_id": self.call}):
            self.assertEqual(result["state"], "unknown", result)

    def test_large_history_tail_uses_call_metadata_and_discards_partial_utf8_prefix(self) -> None:
        header = (json.dumps(self.prefix[0]) + "\n").encode()
        early = (json.dumps(self.prefix[1]) + "\n").encode()
        filler_header = b'{"type":"progress","payload":"'
        filler = ("é" * (9 * 1024 * 1024)).encode("utf-8")
        suffix = ("\n".join(json.dumps(row) for row in self.waiting()[-2:]) + "\n").encode()
        data = header + early + filler_header + filler + b'"}\n' + suffix
        cut = len(data) - 16 * 1024 * 1024
        if data[cut] & 0xC0 != 0x80:
            data = header + early + filler_header + filler + b'x"}\n' + suffix
            cut = len(data) - 16 * 1024 * 1024
        self.assertEqual(data[cut] & 0xC0, 0x80, "tail must start inside a UTF-8 codepoint")
        self.path.write_bytes(data)
        self.assert_state(self.probe_existing(), "waiting", remaining=1)
        compacted = {"type": "compacted", "payload": {"message": "x" * (1024 * 1024 + 4096)}}
        self.assertGreater(len(json.dumps(compacted).encode("utf-8")), 1024 * 1024)
        self.assert_state(self.probes(self.waiting() + [compacted]), "waiting", remaining=1)
        progress = {"type": "progress", "payload": "x" * 180}
        history = self.waiting() + [progress] * 50_000
        self.assert_state(self.probes(history), "waiting", remaining=1)
        self.assert_state(self.probes(history, suffix=b'{"type":"progress","payload":"x"} invalid\n'),
                          "unknown")

    def test_uncovered_expected_call_is_unknown_and_does_not_infer_answer(self) -> None:
        header = ("\n".join(json.dumps(row) for row in self.waiting()) + "\n").encode()
        self.path.write_bytes(header + b'{"type":"progress","payload":"' +
                              b'x' * (17 * 1024 * 1024) + b'"}\n')
        for result in self.probe_existing(expected={"turn_id": self.turn, "call_id": self.call}):
            self.assertEqual(result["state"], "unknown", result)

    def test_malformed_irrelevant_prefix_cannot_hide_a_later_genuine_call(self) -> None:
        self.assert_state(self.probes(self.prefix + ['not-json'] + self.waiting()[-2:]), "waiting", remaining=1)
        other = "call_after_malformed_" + uuid.uuid4().hex
        results = self.probes(self.waiting() + [{"type": "response_item", "payload": []},
                             codex_call(self.turn, other), codex_output(self.turn, other)])
        self.assert_state(results, "waiting", remaining=1)
        for result in results:
            self.assertEqual(result["call_id"], other,
                             "a newer proven call must not inherit an older invalid-prefix offset")

    def test_python_resolved_history_eviction_keeps_current_question_and_pruned_expected_unknown(self) -> None:
        records = list(self.prefix)
        for index in range(129):
            call = f"call_resolved_history_{index:03d}"
            records += [codex_call(self.turn, call), codex_output(self.turn, call),
                        codex_reply(self.turn, call, single=True)]
        records += self.waiting()[-2:]
        write_history(self.path, records)
        result = self.notifier.codex_attention_state(self.path, self.thread)
        self.assert_state([result], "waiting", remaining=1)
        self.assertEqual(result["call_id"], self.call)
        old = self.notifier.codex_attention_state(self.path, self.thread,
                  expected={"turn_id": self.turn, "call_id": "call_resolved_history_000"})
        self.assertEqual(old["state"], "unknown", old)
        replayed = "call_resolved_history_000"
        append_history(self.path, [codex_call(self.turn, replayed), codex_output(self.turn, replayed)])
        replay = self.notifier.codex_attention_state(self.path, self.thread)
        self.assert_state([replay], "waiting", remaining=1)
        self.assertEqual(replay["call_id"], self.call,
                         "an evicted resolved call must not revive on inherited call/ACK replay")
        old = self.notifier.codex_attention_state(self.path, self.thread,
                  expected={"turn_id": self.turn, "call_id": replayed})
        self.assertEqual(old["state"], "unknown", old)

    def test_spoofed_text_wrong_session_subagent_and_invalid_snapshots_never_alert(self) -> None:
        cases = [self.prefix + [codex_user(self.turn, json.dumps(codex_call(self.turn, self.call)))],
                 [{"type": "session_meta", "payload": {"id": str(uuid.uuid4()), "source": "vscode"}},
                  *self.waiting()[1:]],
                 [{"type": "session_meta", "payload": {"id": self.thread,
                                                         "source": {"subagent": {"thread_spawn": {}}}}},
                  *self.waiting()[1:]]]
        for records in cases:
            for result in self.probes(records):
                self.assertNotEqual(result["state"], "waiting", result)
        untrusted_reply = codex_reply(self.turn, self.call, single=True)
        untrusted_reply["payload"]["internal_chat_message_metadata_passthrough"]["content_item_metadata"] = [
            {"provenance": {"type": "tool"}}]
        self.assert_state(self.probes(self.waiting() + [untrusted_reply]), "waiting", remaining=1)
        mixed_reply = codex_reply(self.turn, self.call, single=True)
        mixed_reply["payload"]["content"].append({"type": "input_image", "image_url": "data:image/png;base64,AA=="})
        metadata = mixed_reply["payload"]["internal_chat_message_metadata_passthrough"]
        metadata["content_item_kinds"].append("user.image")
        metadata["content_item_metadata"] = [{"provenance": {"type": "user"}}, None]
        self.assert_state(self.probes(self.waiting() + [mixed_reply]), "resolved", remaining=0)
        expected = {"turn_id": self.turn, "call_id": self.call}
        for suffix in (b'\x80\n', b'{"type":"response_item","payload":'):
            for result in self.probes(self.waiting(), expected=expected, suffix=suffix):
                self.assertEqual(result["state"], "unknown", result)


@unittest.skipUnless(HAS_PS, "Claude attention is a Windows lifecycle integration")
class ClaudeAttentionProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ntfy-claude-attention-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.thread, self.turn = str(uuid.uuid4()), str(uuid.uuid4())
        self.call = "toolu_attention_" + uuid.uuid4().hex
        self.path = self.home / f"{self.thread}.jsonl"
        self.question_id = str(uuid.uuid4())

    def node(self, identity: str, parent: str | None, role: str, content: object, **extra) -> dict:
        return {"type": role, "sessionId": self.thread, "uuid": identity, "parentUuid": parent,
                "isSidechain": False, "message": {"role": role, "content": content}, **extra}

    def waiting(self, *, count=1) -> list[dict]:
        return [self.node(self.turn, None, "user", SECRET_QUESTION),
                self.node(self.question_id, self.turn, "assistant", [{
                    "type": "tool_use", "name": "AskUserQuestion", "id": self.call,
                    "caller": {"type": "direct"},
                    "input": {"questions": [{"question": SECRET_QUESTION, "header": "Choice",
                        "options": [{"label": "A", "description": "First"},
                                    {"label": "B", "description": "Second"}], "multiSelect": False}
                        for _ in range(count)]},
                }])]

    def probe(self, records: list[dict | str], *, expected=None, suffix=b"") -> dict:
        write_history(self.path, records, raw_suffix=suffix)
        result = AttentionProbeTests.ps_probe(self, provider="claude", expected=expected)
        self.assertNotIn(SECRET_QUESTION, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(SECRET_ANSWER, json.dumps(result, ensure_ascii=False))
        return result

    def test_valid_single_call_may_contain_one_to_four_questions(self) -> None:
        for count in (1, 2, 4):
            with self.subTest(count=count):
                result = self.probe(self.waiting(count=count))
                self.assertEqual(result["state"], "waiting", result)
                self.assertEqual(result["turn_id"], self.turn)
                self.assertEqual(result["call_id"], self.call)
                self.assertEqual(result["pending_count"], count)

    def test_exact_tool_result_empty_error_and_new_foreground_input_resolve(self) -> None:
        for content in ("", SECRET_ANSWER):
            with self.subTest(empty=not content):
                answer = self.node(str(uuid.uuid4()), self.question_id, "user", [{
                    "type": "tool_result", "tool_use_id": self.call, "content": content, "is_error": True}])
                self.assertEqual(self.probe(self.waiting() + [answer],
                    expected={"turn_id": self.turn, "call_id": self.call})["state"], "resolved")
        new_user = self.node(str(uuid.uuid4()), self.question_id, "user", "New request")
        self.assertEqual(self.probe(self.waiting() + [new_user],
            expected={"turn_id": self.turn, "call_id": self.call})["state"], "resolved")

    def test_abandoned_branch_and_explicit_leaf_selection(self) -> None:
        records = self.waiting()
        current_user, current_final = str(uuid.uuid4()), str(uuid.uuid4())
        records += [self.node(current_user, self.turn, "user", "Other branch"),
                    self.node(current_final, current_user, "assistant", [{"type": "text", "text": "Final"}]),
                    {"type": "last-prompt", "sessionId": self.thread, "explicit": True,
                     "leafUuid": current_final}]
        self.assertNotEqual(self.probe(records)["state"], "waiting")
        result = self.probe(records + [{"type": "last-prompt", "sessionId": self.thread,
                                       "explicit": True, "leafUuid": self.question_id}])
        self.assertEqual(result["state"], "waiting", result)

    def test_generic_permission_spoof_nested_sidechain_and_batched_tools_never_alert(self) -> None:
        generic = self.waiting()
        generic[-1]["message"]["content"][0]["name"] = "Bash"
        sidechain = self.waiting()
        sidechain[-1]["isSidechain"] = True
        batched = self.waiting()
        batched[-1]["message"]["content"].append({"type": "tool_use", "name": "Bash", "id": "toolu_bash",
                                                  "input": {"command": "echo ok"}})
        nested = self.waiting()
        nested[-1]["message"]["content"][0]["caller"] = {"type": "tool", "tool_id": "outer"}
        spoof = [self.node(self.turn, None, "user", json.dumps(self.waiting()[-1]))]
        error = self.waiting() + [self.node(str(uuid.uuid4()), self.question_id, "assistant",
                    [{"type": "text", "text": "Provider error"}], isApiErrorMessage=True)]
        for records in (generic, sidechain, batched, nested, spoof, error):
            with self.subTest(case=str(records[-1].get("type"))):
                result = self.probe(records)
                self.assertNotEqual(result["state"], "waiting", result)

    def test_wrong_session_incomplete_ancestry_and_partial_snapshot_are_unknown(self) -> None:
        wrong_session = self.waiting()
        wrong_session[-1]["sessionId"] = str(uuid.uuid4())
        missing_parent = self.waiting()[1:]
        missing_parent[-1]["parentUuid"] = str(uuid.uuid4())
        malformed_sidechain = self.waiting()
        malformed_sidechain[-1]["isSidechain"] = "false"
        for records in (wrong_session, missing_parent, malformed_sidechain):
            self.assertNotEqual(self.probe(records)["state"], "waiting")
        self.assertEqual(self.probe(self.waiting(), suffix=b'{"type":"user","message":')["state"], "unknown")


class AttentionDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        contracts.NotifierContractTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        contracts.NotifierContractTests.tearDownClass()

    def fixture(self):
        fixture = contracts.NotifierContractTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.configure(idle_detection_mode="strict", idle_grace_seconds=0,
                          idle_probe_grace_seconds=30, goal_poll_seconds=0.05,
                          suppress_technical_turns=True, watch_rollouts=False,
                          attention_notifications=True, attention_priority=4, priority=3,
                          include_thread_title=True, include_message=True)
        def command_diagnostics(*, probe_ps=False):
            files = {name: [path.name for path in islice((fixture.state / name).glob("*.json"), 129)]
                     for name in ("pending", "outbox", "sent", "suppressed", "dead",
                                  "attention-pending", "attention-sent")}
            details = []
            for path in islice((fixture.state / "attention-pending").glob("*.json"), 16):
                try:
                    if path.stat().st_size > 65536:
                        details.append({"file": path.name, "error": "oversized diagnostic record"})
                        continue
                    record = json.loads(read_text_shared_delete(path))
                    detail = {"file": path.name, "record": record}
                    if probe_ps and len(details) < 4 and isinstance(record, dict):
                        try:
                            reader = AttentionProbeTests(methodName="runTest")
                            reader.path, reader.thread = Path(record["transcript_path"]), record["thread_id"]
                            detail["pure_probe"] = reader.ps_probe(provider=record["provider"],
                                expected={"turn_id": record["turn_id"], "call_id": record["call_id"]})
                        except Exception as error:
                            detail["pure_probe_error"] = str(error)
                    details.append(detail)
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    details.append({"file": path.name, "error": str(error)})
            try:
                log = read_text_shared_delete(fixture.state / "notify.log", errors="replace")
            except OSError as error:
                log = f"<log unavailable: {error}>"
            return f"state={json.dumps(files)}\nattention_pending={json.dumps(details)}\nnotify.log={log[-8000:]}"
        def run_ok(command, *, timeout=30):
            try:
                result = subprocess.run(command, env=fixture.env, text=True, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                # subprocess.run has killed/reaped before the exception reaches us.
                print(f"command timeout={error.timeout}\nstdout={error.stdout!r}\nstderr={error.stderr!r}"
                      f"\n{command_diagnostics(probe_ps='powershell.exe' in command[0].lower())}",
                      file=sys.stderr, flush=True)
                raise
            if result.returncode != 0:
                self.fail(f"command rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
                          f"\n{command_diagnostics()}")
            return result
        fixture.run_ok = run_ok
        def failure_diagnostics():
            outcome = getattr(self, "_outcome", None)
            result = getattr(outcome, "result", None)
            failures = list(getattr(result, "failures", ())) + list(getattr(result, "errors", ()))
            if any(test.id().startswith(self.id()) for test, _trace in failures):
                try:
                    print(f"ATTENTION_FAILURE_DIAGNOSTICS {self.id()}\n{command_diagnostics()}",
                          file=sys.stderr, flush=True)
                except Exception as error:
                    print(f"ATTENTION_FAILURE_DIAGNOSTICS unavailable: {error}", file=sys.stderr, flush=True)
        # Cleanup is LIFO: preserve failure evidence before fixture.tearDown.
        self.addCleanup(failure_diagnostics)
        return fixture

    @staticmethod
    def implementations():
        return ["python"] + (["powershell"] if HAS_PS else [])

    def create_question(self, fixture, *, count=1, subagent=False):
        thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
        call = "call_delivery_" + uuid.uuid4().hex
        path = fixture.write_session_meta(thread, subagent=subagent)
        with closing(sqlite3.connect(fixture.state_database)) as database:
            if "name" not in {row[1] for row in database.execute("PRAGMA table_info(threads)")}:
                database.execute("ALTER TABLE threads ADD COLUMN name TEXT")
            source = json.dumps({"subagent": {}}) if subagent else "vscode"
            database.execute("INSERT INTO threads(id,rollout_path,source,thread_source,name) VALUES (?,?,?,?,?)",
                             (thread, str(path), source, "subagent" if subagent else "user", "Original name"))
            database.commit()
        append_history(path, [event("task_started", turn), event("user_message", turn, message="Choose"),
                              codex_call(turn, call, count=count), codex_output(turn, call)])
        return thread, turn, call, path

    def scan(self, fixture, implementation):
        command = ([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "src/notify-ntfy.ps1"), "-ScanRollouts", "-ScanScope", "Local"]
                   if implementation == "powershell" else
                   [sys.executable, str(ROOT / "src/notify-ntfy.py"), "--scan-rollouts"])
        fixture.run_ok(command, timeout=60)

    def payloads(self, fixture):
        with fixture.server.lock:
            return list(fixture.server.payloads)

    def assert_no_question_storage(self, fixture):
        # Called only after workers are stopped/one-shot commands have exited.
        for path in fixture.state.rglob("*.json"):
            text = read_text_shared_delete(path)
            self.assertNotIn(SECRET_QUESTION, text, path.name)
            self.assertNotIn(SECRET_ANSWER, text, path.name)

    def test_scanner_discovers_question_without_completion_and_renames_at_delivery(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                thread, turn, call, path = self.create_question(fixture)
                self.scan(fixture, implementation)
                pending = list((fixture.state / "attention-pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                queued = json.loads(read_text_shared_delete(pending[0]))
                expected_key = hashlib.sha256(
                    f"codex-ntfy/attention/v1|codex|{thread}|{turn}|{call}".encode()).hexdigest()
                self.assertEqual(queued["key"], expected_key)
                self.assertEqual(pending[0].stem, expected_key)
                self.assertEqual(queued["kind"], "attention")
                self.assertEqual(queued["schema"], 1)
                self.assertEqual(list((fixture.state / "pending").glob("*.json")), [])
                self.assert_no_question_storage(fixture)
                name = "Nome attuale é 漢字"
                with closing(sqlite3.connect(fixture.state_database)) as database:
                    database.execute("UPDATE threads SET name=? WHERE id=?", (name, thread))
                    database.commit()
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                payloads = self.payloads(fixture)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0]["title"], name)
                self.assertEqual(payloads[0]["tags"], ["question"])
                self.assertEqual(payloads[0]["priority"], 4)
                self.assertEqual(payloads[0]["message"], "Reply needed")
                self.assertEqual(list((fixture.state / "sent").glob("*.json")), [])
                self.assertEqual(len(list((fixture.state / "attention-sent").glob("*.json"))), 1)
                self.assert_no_question_storage(fixture)
                if implementation == "python":
                    with mock.patch.dict(os.environ, fixture.env):
                        notifier = load_notifier()
                        counts = notifier.attention_delivery_counts(notifier.Runtime())
                    self.assertEqual(counts["pending"], 0, counts)
                    self.assertEqual(counts["waiting"], 1, counts)

    def test_history_copy_resume_and_old_attention_receipt_do_not_repeat(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                thread, turn, call, path = self.create_question(fixture)
                self.scan(fixture, implementation)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                receipts = list((fixture.state / "attention-sent").glob("*.json"))
                self.assertEqual(len(receipts), 1)
                old = time.time() - 60 * 24 * 3600
                os.utime(receipts[0], (old, old))
                resumed = path.parent / ("resumed-" + path.name)
                shutil.copyfile(path, resumed)
                with closing(sqlite3.connect(fixture.state_database)) as database:
                    database.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(resumed), thread))
                    database.commit()
                path.unlink()  # a resumed path must not depend on the old file remaining readable
                self.scan(fixture, implementation)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(len(self.payloads(fixture)), 1)
                self.assertTrue(receipts[0].exists(), "attention dedup must not use generic sent TTL")
                append_history(resumed, [codex_reply(turn, call), codex_call(turn, "call_next"),
                                         codex_output(turn, "call_next")])
                self.scan(fixture, implementation)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(len(self.payloads(fixture)), 2)

    def test_answer_before_dispatch_cancels_attention_without_completion_authority(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                thread, turn, call, path = self.create_question(fixture)
                self.scan(fixture, implementation)
                append_history(path, [codex_reply(turn, call, single=True, answer="")])
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(self.payloads(fixture), [])
                self.assertEqual(list((fixture.state / "attention-pending").glob("*.json")), [])
                self.assertEqual(list((fixture.state / "attention-sent").glob("*.json")), [])
                self.assertEqual(list((fixture.state / "sent").glob("*.json")), [])

    def test_answer_during_503_prevents_attention_retry(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                thread, turn, call, path = self.create_question(fixture)
                self.scan(fixture, implementation)
                with fixture.server.lock:
                    fixture.server.statuses = [503, 200]
                    fixture.server.before_response = lambda _payload, status: (
                        append_history(path, [codex_reply(turn, call, single=True)]) if status == 503 else None)
                try:
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                finally:
                    with fixture.server.lock:
                        fixture.server.before_response = None
                self.assertEqual(len(self.payloads(fixture)), 1, "answered question must not be retried")
                self.assertEqual(list((fixture.state / "attention-sent").glob("*.json")), [])
                self.assertEqual(list((fixture.state / "attention-pending").glob("*.json")), [])

    def test_question_pause_is_not_done_but_next_genuine_turn_completes(self) -> None:
        for implementation in self.implementations():
            for same_turn in (True, False):
                with self.subTest(implementation=implementation, same_turn=same_turn):
                    fixture = self.fixture()
                    thread, turn, call, path = self.create_question(fixture)
                    append_history(path, [event("task_complete", turn, last_agent_message="Choose first")])
                    self.scan(fixture, implementation)
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                    self.assertEqual(len(self.payloads(fixture)), 1)
                    self.assertEqual(self.payloads(fixture)[0]["tags"], ["question"])
                    self.assertEqual(list((fixture.state / "sent").glob("*.json")), [])
                    append_history(path, [codex_reply(turn, call)])
                    self.scan(fixture, implementation)
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                    self.assertEqual(len(self.payloads(fixture)), 1,
                                     "reply alone must not revive the terminal preceding that reply")
                    next_turn = turn if same_turn else str(uuid.uuid4())
                    append_history(path, [event("task_started", next_turn),
                        event("user_message", next_turn, message="Continue after my answer"),
                        event("task_complete", next_turn, last_agent_message="Completed after answer")])
                    self.scan(fixture, implementation)
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                    payloads = self.payloads(fixture)
                    self.assertEqual(len(payloads), 2)
                    self.assertEqual(payloads[1]["tags"], ["white_check_mark"])
                    self.assertEqual(payloads[1].get("priority", 3), 3)
                    self.assertEqual(len(list((fixture.state / "sent").glob("*.json"))), 1)

    def test_subagent_and_disabled_attention_do_not_send(self) -> None:
        for implementation in self.implementations():
            for disabled in (False, True):
                with self.subTest(implementation=implementation, disabled=disabled):
                    fixture = self.fixture()
                    self.create_question(fixture, subagent=not disabled)
                    if disabled:
                        fixture.configure(attention_notifications=False)
                    self.scan(fixture, implementation)
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                    self.assertEqual(self.payloads(fixture), [])

    def test_off_mode_question_pause_is_distinct_and_disable_restores_legacy_completion(self) -> None:
        for implementation in self.implementations():
            for enabled in (True, False):
                with self.subTest(implementation=implementation, enabled=enabled):
                    fixture = self.fixture()
                    fixture.configure(idle_detection_mode="off", attention_notifications=enabled)
                    thread, turn, call, path = self.create_question(fixture)
                    append_history(path, [event("task_complete", turn, last_agent_message="Paused for a question")])
                    self.scan(fixture, implementation)
                    fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                    payloads = self.payloads(fixture)
                    self.assertEqual(len(payloads), 1)
                    self.assertEqual(payloads[0]["tags"], ["question"] if enabled else ["white_check_mark"])
                    self.assertEqual(len(list((fixture.state / "sent").glob("*.json"))), 0 if enabled else 1)

    def test_active_goal_does_not_hide_attention_or_complete_ordinary_work(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                thread, turn, call, path = self.create_question(fixture)
                fixture.create_goal_database(thread, "active")
                append_history(path, [event("task_complete", turn, last_agent_message="Question while goal active")])
                self.scan(fixture, implementation)
                process = subprocess.Popen(fixture.continuous_worker_command(implementation), env=fixture.env,
                                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    payloads = fixture.wait_for_payloads(1, timeout=60)
                    self.assertEqual(len(payloads), 1)
                    self.assertEqual(payloads[0]["tags"], ["question"])
                finally:
                    fixture.stop_continuous_worker(process)
                self.assertEqual(list((fixture.state / "sent").glob("*.json")), [])
                self.assert_no_question_storage(fixture)

    def test_disabling_after_enqueue_is_silent_and_reenable_revalidates_current_question(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                self.create_question(fixture)
                self.scan(fixture, implementation)
                fixture.configure(attention_notifications=False)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(self.payloads(fixture), [])
                fixture.configure(attention_notifications=True)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(len(self.payloads(fixture)), 1)

    def test_initial_old_question_is_not_replayed_and_unchanged_cursor_is_not_rewritten(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                fixture = self.fixture()
                fixture.configure(watch_initial_replay_seconds=0)
                thread, turn, call, path = self.create_question(fixture)
                old = time.time() - 3600
                os.utime(path, (old, old))
                self.scan(fixture, implementation)
                cursors = list((fixture.state / "watch").glob("*.json"))
                before = {item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in cursors}
                self.assertEqual(list((fixture.state / "attention-pending").glob("*.json")), [])
                self.scan(fixture, implementation)
                after = {item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                         for item in (fixture.state / "watch").glob("*.json")}
                self.assertEqual(after, before)
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(self.payloads(fixture), [])

    def test_malformed_attention_authority_never_sends_or_blocks_unrelated_delivery(self) -> None:
        for implementation in self.implementations():
            for area in ("attention-pending", "attention-sent"):
                with self.subTest(implementation=implementation, malformed=area):
                    fixture = self.fixture()
                    fixture.configure(idle_detection_mode="off")
                    self.create_question(fixture)
                    self.scan(fixture, implementation)
                    pending = list((fixture.state / "attention-pending").glob("*.json"))
                    self.assertEqual(len(pending), 1)
                    target = fixture.state / area / pending[0].name
                    target.parent.mkdir(exist_ok=True)
                    target.write_text('{"invalid":', encoding="utf-8")
                    fixture.run_ok(fixture.hook_command(implementation, fixture.event()))
                    process = subprocess.Popen(fixture.continuous_worker_command(implementation), env=fixture.env,
                                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        payloads = fixture.wait_for_payloads(1, timeout=60)
                        self.assertEqual(len(payloads), 1)
                        self.assertEqual(payloads[0]["tags"], ["white_check_mark"])
                    finally:
                        fixture.stop_continuous_worker(process)


@unittest.skipUnless(HAS_PS, "Claude attention is a Windows lifecycle integration")
class ClaudeAttentionDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        contracts.NotifierContractTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        contracts.NotifierContractTests.tearDownClass()

    def fixture(self):
        return AttentionDeliveryTests.fixture(self)

    def create_question(self, fixture, *, count=1):
        builder = ClaudeAttentionProbeTests(methodName="runTest")
        builder.thread, builder.turn = str(uuid.uuid4()), str(uuid.uuid4())
        builder.call = "toolu_delivery_" + uuid.uuid4().hex
        builder.question_id = str(uuid.uuid4())
        path = fixture.temp / f"{builder.thread}.jsonl"
        write_history(path, builder.waiting(count=count))
        notice = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                  "session_id": builder.thread, "transcript_path": str(path),
                  "cwd": r"C:\work\attention", "message": "Generic permission UI text must not be trusted"}
        return builder, path, notice

    def payloads(self, fixture):
        return AttentionDeliveryTests.payloads(self, fixture)

    def test_permission_and_pretool_resume_without_prompt_id_send_one_distinct_alert(self) -> None:
        fixture = self.fixture()
        builder, path, notice = self.create_question(fixture, count=2)
        normal = fixture.claude_event(session_id=builder.thread, prompt_id=str(uuid.uuid4()), transcript_path=path)
        self.assertNotEqual(normal["prompt_id"], builder.turn,
                            "runtime prompt ID must not be confused with durable user-node UUID")
        fixture.run_claude_hook(fixture.claude_prompt_event(normal))
        fixture.run_claude_hook(normal)  # an erroneous/intermediate Stop while the Ask is waiting
        states_before = {item.name: item.read_bytes()
                         for item in (fixture.state / "claude-sessions").glob("*.json")}
        self.assertEqual(len(states_before), 1)
        pre = {**notice, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
               "tool_use_id": builder.call, "tool_input": builder.waiting(count=2)[-1]["message"]["content"][0]["input"]}
        pre.pop("notification_type")
        fixture.run_claude_hook(pre)
        fixture.run_claude_hook(notice)
        append_history(path, [{"type": "custom-title", "sessionId": builder.thread,
                               "customTitle": "Domanda attuale é 漢字"}])
        fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        payloads = self.payloads(fixture)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["title"], "Domanda attuale é 漢字")
        self.assertEqual(payloads[0]["tags"], ["question"])
        self.assertEqual(payloads[0]["priority"], 4)
        self.assertEqual(payloads[0]["message"], "Reply needed")
        self.assertEqual(list((fixture.state / "sent").glob("*.json")), [])
        states_after = {item.name: item.read_bytes()
                        for item in (fixture.state / "claude-sessions").glob("*.json")}
        self.assertEqual(states_after, states_before, "question must not mutate busy/idle completion state")
        fixture.run_claude_hook(notice)  # genuine resume still has no hook prompt_id
        fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        self.assertEqual(len(self.payloads(fixture)), 1)
        answer_id = str(uuid.uuid4())
        answer = builder.node(answer_id, builder.question_id, "user", [{
            "type": "tool_result", "tool_use_id": builder.call, "content": SECRET_ANSWER}])
        final = builder.node(str(uuid.uuid4()), answer_id, "assistant", [{"type": "text", "text": "Final after answer"}])
        final["message"]["stop_reason"] = "end_turn"
        append_history(path, [answer])
        fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        self.assertEqual(len(self.payloads(fixture)), 1,
                         "answer alone cannot revive the paused completion")
        append_history(path, [final])
        normal["last_assistant_message"] = "Final after answer"
        fixture.run_claude_hook(normal)
        fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        payloads = self.payloads(fixture)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[1]["tags"], ["white_check_mark"])
        self.assertEqual(len(list((fixture.state / "sent").glob("*.json"))), 1)
        AttentionDeliveryTests.assert_no_question_storage(self, fixture)

    def test_answer_during_failure_prevents_retry_and_new_call_can_alert(self) -> None:
        fixture = self.fixture()
        builder, path, notice = self.create_question(fixture)
        fixture.run_claude_hook(notice)
        answer_id = str(uuid.uuid4())
        answer = builder.node(answer_id, builder.question_id, "user", [{
            "type": "tool_result", "tool_use_id": builder.call, "content": SECRET_ANSWER}])
        with fixture.server.lock:
            fixture.server.statuses = [503, 200]
            fixture.server.before_response = lambda _payload, status: append_history(path, [answer]) if status == 503 else None
        try:
            fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        finally:
            with fixture.server.lock:
                fixture.server.before_response = None
        self.assertEqual(len(self.payloads(fixture)), 1)
        self.assertEqual(list((fixture.state / "attention-sent").glob("*.json")), [])
        next_question = builder.waiting()[-1]
        next_question["uuid"] = str(uuid.uuid4())
        next_question["parentUuid"] = answer_id
        next_question["message"]["content"][0]["id"] = "toolu_new_question"
        append_history(path, [next_question])
        fixture.run_claude_hook(notice)
        fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
        self.assertEqual(len(self.payloads(fixture)), 2)

    def test_generic_permission_subagent_and_already_populated_answers_are_silent(self) -> None:
        for kind in ("bash", "subagent", "answered-input", "batch"):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                builder, path, notice = self.create_question(fixture)
                if kind in ("bash", "batch"):
                    records = builder.waiting()
                    bash = {"type": "tool_use", "name": "Bash", "id": "toolu_bash",
                            "input": {"command": "echo AskUserQuestion"}, "caller": {"type": "direct"}}
                    if kind == "bash":
                        records[-1]["message"]["content"] = [bash]
                    else:
                        records[-1]["message"]["content"].append(bash)
                    write_history(path, records)
                elif kind == "subagent":
                    notice["agent_id"] = "nested-agent"
                else:
                    notice.update(hook_event_name="PreToolUse", tool_name="AskUserQuestion",
                                  tool_use_id=builder.call,
                                  tool_input={**builder.waiting()[-1]["message"]["content"][0]["input"],
                                              "answers": {SECRET_QUESTION: SECRET_ANSWER}})
                fixture.run_claude_hook(notice)
                fixture.run_ok(fixture.worker_command("powershell"), timeout=60)
                self.assertEqual(self.payloads(fixture), [])


if __name__ == "__main__":
    unittest.main()
