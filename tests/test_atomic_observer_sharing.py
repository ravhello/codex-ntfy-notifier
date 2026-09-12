"""Real Windows handles show why queue observers must share deletion.

These tests exercise the existing atomic writers without changing their error
policy. An observer must not create the sharing violation it is observing.
"""

from __future__ import annotations

import codecs
from contextlib import contextmanager, nullcontext
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid

from notifier_test_io import read_text_shared_delete


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = (
    Path(os.environ.get("WINDIR", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)

HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  $env:ATOMIC_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
foreach ($name in @('ConvertTo-CompactJson', 'Write-JsonAtomic')) {
  $definition = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true))
  if ($definition.Count -ne 1) { throw "function not unique: $name" }
  . ([scriptblock]::Create($definition[0].Extent.Text))
}
try {
  Write-JsonAtomic -Path $env:ATOMIC_TARGET -Value ($env:ATOMIC_VALUE | ConvertFrom-Json)
  @{ ok = $true } | ConvertTo-Json -Compress
} catch {
  $codes = @()
  $exception = $_.Exception
  while ($null -ne $exception) {
    $codes += ($exception.HResult -band 0xffff)
    $exception = $exception.InnerException
  }
  @{ ok = $false; codes = @($codes); error = [string]$_ } | ConvertTo-Json -Compress
}
"""


@contextmanager
def deny_delete_reader(path: Path):
    """A real Windows read handle, deliberately lacking FILE_SHARE_DELETE."""
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


@unittest.skipUnless(os.name == "nt", "Windows file-sharing semantics")
class AtomicObserverSharingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "atomic_observer_notifier", ROOT / "src" / "notify-ntfy.py"
        )
        assert spec is not None and spec.loader is not None
        cls.notifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.notifier)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ntfy-observer-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pending.json"
        self.old_text = json.dumps(
            {"state": "old", "message": "old-é-" * 24000}, ensure_ascii=False
        )
        self.old_bytes = self.old_text.encode("utf-8")
        self.path.write_bytes(self.old_bytes)
        self.new_value = {"state": "new", "message": "complete-é"}

    def write_actual(self, implementation: str) -> dict:
        if implementation == "python":
            try:
                self.notifier.atomic_write_json(self.path, self.new_value)
            except OSError as error:
                return {"ok": False, "codes": [error.winerror], "error": str(error)}
            return {"ok": True}
        if not POWERSHELL.exists():
            self.skipTest("Windows PowerShell is unavailable")
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", HARNESS],
            env={
                **os.environ,
                "ATOMIC_NOTIFIER": str(ROOT / "src" / "notify-ntfy.ps1"),
                "ATOMIC_TARGET": str(self.path),
                "ATOMIC_VALUE": json.dumps(self.new_value),
            },
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    def assert_deny_delete_blocks_writer(self, implementation: str) -> None:
        with deny_delete_reader(self.path) as reader:
            result = self.write_actual(implementation)
            self.assertFalse(result["ok"], result)
            # PowerShell's fallback Move-Item surfaces ERROR_ALREADY_EXISTS.
            self.assertTrue(set(result["codes"]) & {5, 32, 33, 183}, result)
            self.assertEqual(reader.read(), self.old_bytes)
            self.assertEqual(self.path.read_bytes(), self.old_bytes)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])
        # Releasing only the observer's handle is enough for the same writer.
        result = self.write_actual(implementation)
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(read_text_shared_delete(self.path)), self.new_value)

    def assert_shared_delete_allows_writer(self, implementation: str) -> None:
        entered = threading.Event()
        release = threading.Event()
        attempted = threading.Event()
        finished = threading.Event()
        results = []
        writes = []
        errors = []
        codec_name = "observer_utf8_" + uuid.uuid4().hex
        utf8 = codecs.lookup("utf-8")
        mutation_locks = self.path.parent / "mutation-locks"
        mutation_locks.mkdir()
        runtime = SimpleNamespace(ensure=lambda: None, mutation_locks=mutation_locks)
        key = "a" * 64

        def guard():
            # Python's MoveFileEx-based os.replace rejects an open destination
            # even with share-delete. Its observer joins the same real key lock
            # as the pending writer, closing the read handle before unlocking.
            if implementation == "python":
                return self.notifier.record_mutation_lock(runtime, key)
            return nullcontext()

        class HeldUtf8Decoder(utf8.incrementaldecoder):
            def decode(self, data, final=False):
                # The helper's genuine file handle remains open inside read().
                # Only decoding is synchronized; filesystem calls are untouched.
                if data:
                    entered.set()
                    if not release.wait(45):
                        raise TimeoutError("writer did not release the observer")
                return super().decode(data, final)

        def lookup(name):
            if name == codec_name:
                return codecs.CodecInfo(
                    name=codec_name,
                    encode=utf8.encode,
                    decode=utf8.decode,
                    incrementalencoder=utf8.incrementalencoder,
                    incrementaldecoder=HeldUtf8Decoder,
                )
            return None

        def observe():
            try:
                with guard():
                    results.append(read_text_shared_delete(self.path, encoding=codec_name))
            except BaseException as error:
                errors.append(error)

        def mutate():
            try:
                attempted.set()
                with guard():
                    writes.append(self.write_actual(implementation))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        codecs.register(lookup)
        reader = threading.Thread(target=observe, daemon=True)
        writer = threading.Thread(target=mutate, daemon=True)
        reader.start()
        try:
            self.assertTrue(entered.wait(10), f"observer did not open the file: {errors}")
            writer.start()
            self.assertTrue(attempted.wait(5), "writer did not start")
            self.assertTrue(reader.is_alive())
            if implementation == "python":
                self.assertFalse(finished.wait(0.1), "writer bypassed the observer's key lock")
                self.assertEqual(read_text_shared_delete(self.path), self.old_text)
            else:
                self.assertTrue(finished.wait(35), "writer did not finish")
                self.assertEqual(errors, [])
                self.assertEqual(writes, [{"ok": True}])
                # File.Replace can publish the new file with the old reader open.
                self.assertEqual(json.loads(read_text_shared_delete(self.path)), self.new_value)
        finally:
            release.set()
            reader.join(10)
            if writer.ident is not None:
                writer.join(35)
            codecs.unregister(lookup)
        self.assertFalse(reader.is_alive(), "observer did not finish")
        self.assertFalse(writer.is_alive(), "writer did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(results, [self.old_text])
        self.assertEqual(writes, [{"ok": True}])
        self.assertEqual(json.loads(read_text_shared_delete(self.path)), self.new_value)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_python_deny_delete_observer_blocks_atomic_replacement(self) -> None:
        self.assert_deny_delete_blocks_writer("python")

    def test_powershell_deny_delete_observer_blocks_atomic_replacement(self) -> None:
        self.assert_deny_delete_blocks_writer("powershell")

    def test_python_locked_observer_preserves_both_complete_versions(self) -> None:
        self.assert_shared_delete_allows_writer("python")

    def test_powershell_shared_delete_observer_preserves_both_complete_versions(self) -> None:
        self.assert_shared_delete_allows_writer("powershell")


if __name__ == "__main__":
    unittest.main()
