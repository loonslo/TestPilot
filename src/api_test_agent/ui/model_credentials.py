"""Store Web model API keys in the current Windows user's Credential Manager."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_GENERIC = 1
_LOCAL_MACHINE = 2
_NOT_FOUND = 1168
MAX_KEY_LENGTH = 2560


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def read_key(target: str) -> str | None:
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    pointer = ctypes.POINTER(_Credential)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
    ]
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    stored = pointer()
    if not api.CredReadW(target, _GENERIC, 0, ctypes.byref(stored)):
        if ctypes.get_last_error() == _NOT_FOUND:
            return None
        raise ValueError("无法读取 Windows 凭据管理器中的模型 API Key。")
    try:
        field = stored.contents
        return ctypes.string_at(field.CredentialBlob, field.CredentialBlobSize).decode("ascii")
    finally:
        api.CredFree(stored)


def write_key(target: str, key: str | None) -> None:
    if os.name != "nt":
        raise ValueError("网页保存 API Key 需要 Windows 凭据管理器。请使用环境变量配置。")
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    if key is None:
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        if not api.CredDeleteW(target, _GENERIC, 0) and ctypes.get_last_error() != _NOT_FOUND:
            raise ValueError("无法移除 Windows 凭据管理器中的模型 API Key。")
        return
    data = key.encode("ascii")
    if not 1 <= len(data) <= MAX_KEY_LENGTH:
        raise ValueError("模型 API Key 长度不符合要求。")
    blob = ctypes.create_string_buffer(data)
    credential = _Credential(
        Type=_GENERIC,
        TargetName=target,
        CredentialBlobSize=len(data),
        CredentialBlob=ctypes.cast(blob, ctypes.c_void_p),
        Persist=_LOCAL_MACHINE,
        UserName="TestPilot model API key",
    )
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ValueError("无法把模型 API Key 保存到 Windows 凭据管理器。")
    finally:
        ctypes.memset(blob, 0, len(blob))
