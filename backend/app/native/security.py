"""Windows installation boundaries and exact, protected private-data ACLs."""
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import stat


SID = r'S-1-\d+(?:-\d+)+'


def reject_reparse(path):
    """Check the raw spelling before resolve() can hide a junction ancestor."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native_absolute_nontraversing_path_required')
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('native_reparse_path_not_allowed')
    return path


def tree_entries(root):
    """Enumerate without following links, including Windows junctions."""
    root = reject_reparse(root)
    for entry in root.iterdir():
        reject_reparse(entry)
        yield entry
        if entry.is_dir():
            yield from tree_entries(entry)


def installation_root(wrapper):
    wrapper = reject_reparse(wrapper)
    expected = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Tianyan'
    reject_reparse(expected)
    if wrapper.name != 'TianyanService.exe' or wrapper.parent.resolve() != expected.resolve():
        raise ValueError('native_service_wrapper_location_invalid')
    return expected.resolve(strict=True)


def _libraries():
    if os.name != 'nt':
        raise RuntimeError('windows_installer_required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    return kernel, advapi


def _token_sid(process):
    kernel, advapi = _libraries()
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            kernel.LocalFree(value)
    finally:
        kernel.CloseHandle(token)


def current_sid():
    kernel, _ = _libraries()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    return _token_sid(kernel.GetCurrentProcess())


def shell_owner_sid():
    """Use the desktop owner even when UAC ran as a different administrator."""
    kernel, _ = _libraries()
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetShellWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    window = user.GetShellWindow()
    if not window:
        return None  # Headless installation; only here is fallback appropriate.
    identity = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(window, ctypes.byref(identity)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    process = kernel.OpenProcess(0x1000, False, identity)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _token_sid(process)
    finally:
        kernel.CloseHandle(process)


def owner_sid():
    return shell_owner_sid() or current_sid()


def private_sddl(owner, installer):
    if not all(isinstance(value, str) and re.fullmatch(SID, value) for value in (owner, installer)):
        raise ValueError('native_acl_owner_invalid')
    # P disables inheritance. This replaces the entire DACL in one operation;
    # unrelated explicit grants are removed without ever resetting to ProgramData.
    return ('O:' + installer + 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
            '(A;OICI;0x1301bf;;;LS)(A;OICI;FA;;;' + owner + ')')


@contextmanager
def _descriptor(sddl):
    kernel, advapi = _libraries()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    pointer = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(pointer), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield kernel, advapi, pointer
    finally:
        kernel.LocalFree(pointer)


def protect_data(folder, owner):
    folder = reject_reparse(folder)
    if folder == Path(folder.anchor) or not folder.parent.is_dir():
        raise ValueError('native_acl_target_invalid')
    with _descriptor(private_sddl(owner, current_sid())) as (kernel, advapi, descriptor):
        class Attributes(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
        kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
        advapi.SetKernelObjectSecurity.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

        @contextmanager
        def pinned(path, access):
            # No delete sharing pins each parent while children are traversed;
            # OPEN_REPARSE_POINT checks the object itself instead of its target.
            handle = kernel.CreateFileW(str(path), access, 3, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                info = (wintypes.DWORD * 2)()
                if not kernel.GetFileInformationByHandleEx(handle, 9, info, ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError('native_reparse_path_not_allowed')
                yield handle, info[0]
            finally:
                kernel.CloseHandle(handle)

        def secure(path):
            with pinned(path, 0xE0080) as (handle, attributes):
                if not advapi.SetKernelObjectSecurity(handle, 0x80000005, descriptor):
                    raise ctypes.WinError(ctypes.get_last_error())
                if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                    for child in path.iterdir():
                        secure(child)
        with ExitStack() as ancestors:
            for parent in reversed(folder.parents):
                ancestors.enter_context(pinned(parent, 0x80))
            if not folder.exists():
                attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
                if not kernel.CreateDirectoryW(str(folder), ctypes.byref(attributes)):
                    raise ctypes.WinError(ctypes.get_last_error())
            secure(folder)
