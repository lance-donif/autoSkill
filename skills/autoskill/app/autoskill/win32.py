"""Windows ABI bindings, initialized once by the import lock on first use."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as win


class Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", win.DWORD), ("OffsetHigh", win.DWORD), ("hEvent", win.HANDLE)]


class FileBasicInfo(ctypes.Structure):
    _fields_ = [(field, ctypes.c_int64) for field in ("creation", "access", "write", "change")] + [("attributes", win.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", win.DWORD),
                ("minimum_working_set", ctypes.c_size_t), ("maximum_working_set", ctypes.c_size_t),
                ("active_processes", win.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", win.DWORD), ("scheduling", win.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", win.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
overlapped_pointer = ctypes.POINTER(Overlapped)
_bind(kernel, "LockFileEx", win.BOOL, win.HANDLE, win.DWORD, win.DWORD, win.DWORD, win.DWORD, overlapped_pointer)
_bind(kernel, "UnlockFileEx", win.BOOL, win.HANDLE, win.DWORD, win.DWORD, win.DWORD, overlapped_pointer)
_bind(kernel, "CreateFileW", win.HANDLE, win.LPCWSTR, win.DWORD, win.DWORD, win.LPVOID, win.DWORD, win.DWORD, win.HANDLE)
_bind(kernel, "GetFileInformationByHandleEx", win.BOOL, win.HANDLE, ctypes.c_int, win.LPVOID, win.DWORD)
_bind(kernel, "GetVolumeInformationByHandleW", win.BOOL, win.HANDLE, win.LPWSTR, win.DWORD, win.LPDWORD, win.LPDWORD, win.LPDWORD, win.LPWSTR, win.DWORD)
_bind(kernel, "CloseHandle", win.BOOL, win.HANDLE)
_bind(kernel, "OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
_bind(kernel, "GetExitCodeProcess", win.BOOL, win.HANDLE, win.LPDWORD)
_bind(kernel, "GetProcessTimes", win.BOOL, win.HANDLE, *(ctypes.POINTER(win.FILETIME),) * 4)
_bind(kernel, "CreateJobObjectW", win.HANDLE, win.LPVOID, win.LPCWSTR)
_bind(kernel, "SetInformationJobObject", win.BOOL, win.HANDLE, ctypes.c_int, win.LPVOID, win.DWORD)
_bind(kernel, "AssignProcessToJobObject", win.BOOL, win.HANDLE, win.HANDLE)
_bind(kernel, "LocalFree", win.LPVOID, win.LPVOID)

crypt = ctypes.WinDLL("crypt32", use_last_error=True)
blob_pointer = ctypes.POINTER(DataBlob)
_bind(crypt, "CryptProtectData", win.BOOL, blob_pointer, win.LPCWSTR, blob_pointer, win.LPVOID, win.LPVOID, win.DWORD, blob_pointer)
_bind(crypt, "CryptUnprotectData", win.BOOL, blob_pointer, ctypes.POINTER(win.LPWSTR), blob_pointer, win.LPVOID, win.LPVOID, win.DWORD, blob_pointer)

security = ctypes.WinDLL("advapi32", use_last_error=True)
_bind(security, "GetFileSecurityW", win.BOOL, win.LPCWSTR, win.DWORD, win.LPVOID, win.DWORD, win.LPDWORD)
_bind(security, "GetSecurityDescriptorControl", win.BOOL, win.LPVOID, ctypes.POINTER(win.WORD), win.LPDWORD)
_bind(security, "SetFileSecurityW", win.BOOL, win.LPCWSTR, win.DWORD, win.LPVOID)
