"""Read-only test observers that do not deny Windows atomic replacement."""

import os
from pathlib import Path


def read_text_shared_delete(
    path: Path, *, encoding: str = "utf-8-sig", errors: str = "strict"
) -> str:
    if os.name != "nt":
        return path.read_text(encoding=encoding, errors=errors)

    import ctypes
    from ctypes import wintypes
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
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    # open_osfhandle transfers ownership to fd; fdopen owns and closes it.
    try:
        stream = os.fdopen(fd, "r", encoding=encoding, errors=errors)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        return stream.read()
