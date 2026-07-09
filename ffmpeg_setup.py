"""Unduh FFmpeg ke C:\\ffmpeg dan tambahkan C:\\ffmpeg\\bin ke PATH Windows."""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import urllib.request
import winreg
import zipfile
from typing import Callable

FFMPEG_ROOT = r"C:\ffmpeg"
FFMPEG_BIN = r"C:\ffmpeg\bin"
FFMPEG_EXE = r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_ffmpeg() -> str | None:
    """Cari ffmpeg.exe di PATH atau di C:\\ffmpeg\\bin."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.path.isfile(FFMPEG_EXE):
        return FFMPEG_EXE
    return None


def path_entries_from_value(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def read_path_variable(system: bool) -> str:
    if system:
        hive = winreg.HKEY_LOCAL_MACHINE
        key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        hive = winreg.HKEY_CURRENT_USER
        key_path = "Environment"

    with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
            return value or ""
        except FileNotFoundError:
            return ""


def path_contains(path_to_check: str, system: bool) -> bool:
    current = read_path_variable(system)
    target = os.path.normcase(os.path.normpath(path_to_check))
    return target in {os.path.normcase(os.path.normpath(p)) for p in path_entries_from_value(current)}


def is_bin_in_path() -> bool:
    return path_contains(FFMPEG_BIN, system=False) or path_contains(FFMPEG_BIN, system=True)


def notify_environment_change() -> None:
    if os.name != "nt":
        return
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF,
        0x001A,
        0,
        "Environment",
        0x0002,
        5000,
        ctypes.byref(result),
    )


def add_to_path(path_to_add: str, system: bool, log: LogCallback | None = None) -> None:
    if system:
        hive = winreg.HKEY_LOCAL_MACHINE
        key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        scope = "System"
    else:
        hive = winreg.HKEY_CURRENT_USER
        key_path = "Environment"
        scope = "User"

    with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ

        entries = path_entries_from_value(current)
        normalized_new = os.path.normcase(os.path.normpath(path_to_add))
        if normalized_new in {os.path.normcase(os.path.normpath(p)) for p in entries}:
            if log:
                log(f"PATH {scope} sudah berisi {path_to_add}\n")
            return

        new_value = current.rstrip(";")
        if new_value:
            new_value += f";{path_to_add}"
        else:
            new_value = path_to_add

        winreg.SetValueEx(key, "Path", 0, value_type, new_value)
        if log:
            log(f"PATH {scope} diperbarui: ...;{path_to_add}\n")


def refresh_process_path() -> None:
    os.environ["PATH"] = os.environ.get("PATH", "") + f";{FFMPEG_BIN}"


def find_extracted_root(extract_dir: str) -> str:
    for root, _dirs, files in os.walk(extract_dir):
        if "ffmpeg.exe" in files and os.path.basename(root).lower() == "bin":
            return os.path.dirname(root)
    raise FileNotFoundError("ffmpeg.exe tidak ditemukan setelah ekstrak arsip.")


def install_ffmpeg(
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    use_system_path: bool | None = None,
) -> None:
    def write(message: str) -> None:
        if log:
            log(message)

    if os.path.isfile(FFMPEG_EXE):
        write(f"FFmpeg sudah ada di {FFMPEG_EXE}\n")
    else:
        write("Mengunduh FFmpeg (ukuran ~80 MB, mohon tunggu)...\n")
        temp_dir = tempfile.mkdtemp(prefix="ffmpeg_install_")
        zip_path = os.path.join(temp_dir, "ffmpeg-release-essentials.zip")
        extract_dir = os.path.join(temp_dir, "extract")

        try:
            _download_file(FFMPEG_DOWNLOAD_URL, zip_path, progress, write)
            write("Mengekstrak arsip FFmpeg...\n")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(extract_dir)

            source_root = find_extracted_root(extract_dir)
            write(f"Memasang FFmpeg ke {FFMPEG_ROOT}...\n")

            if os.path.exists(FFMPEG_ROOT):
                shutil.rmtree(FFMPEG_ROOT)

            shutil.copytree(source_root, FFMPEG_ROOT)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not os.path.isfile(FFMPEG_EXE):
            raise FileNotFoundError(f"Instalasi gagal: {FFMPEG_EXE} tidak ditemukan.")

        write("FFmpeg berhasil dipasang di C:\\ffmpeg\n")

    if use_system_path is None:
        use_system_path = is_admin()

    if not is_bin_in_path():
        write("Menambahkan C:\\ffmpeg\\bin ke PATH Windows...\n")
        if use_system_path:
            add_to_path(FFMPEG_BIN, system=True, log=log)
        else:
            add_to_path(FFMPEG_BIN, system=False, log=log)
            if not is_admin():
                write(
                    "PATH User diperbarui. Jika ingin PATH System, jalankan aplikasi sebagai Administrator.\n"
                )
        notify_environment_change()
        refresh_process_path()
        write("PATH diperbarui. Perintah FFmpeg siap dipakai.\n")
    else:
        write("C:\\ffmpeg\\bin sudah ada di PATH.\n")
        refresh_process_path()


def _download_file(
    url: str,
    destination: str,
    progress: ProgressCallback | None,
    log: LogCallback | None,
) -> None:
    def report(block_num: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        downloaded = min(block_num * block_size, total_size)
        if progress:
            progress(downloaded, total_size)
        if log and block_num % 256 == 0:
            percent = downloaded * 100 // total_size
            log(f"Unduhan: {percent}%\n")

    urllib.request.urlretrieve(url, destination, reporthook=report)
    if progress:
        progress(1, 1)
