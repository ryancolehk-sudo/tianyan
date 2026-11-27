"""Create the Windows desktop shortcut and open this local instance."""
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import webbrowser

from project import ROOT, compose, config, start_instance, wait_ready


def create_shortcut():
    if os.name != "nt":
        raise SystemExit("桌面快捷方式适用于 Windows。")
    write_link(Path(sys.executable), ROOT)


def write_link(python, root, directory=None):
    # Paths travel as data so spaces, quotes and shell metacharacters stay literal.
    command = r"""
$ErrorActionPreference = 'Stop'
$desktop = $env:TIANYAN_LINK_DIR
if (!$desktop) { $desktop = [Environment]::GetFolderPath('DesktopDirectory') }
if (!(Test-Path -LiteralPath $desktop -PathType Container)) { throw 'Desktop unavailable' }
$shell = New-Object -ComObject WScript.Shell
$arguments = '-B "' + (Join-Path $env:TIANYAN_LINK_ROOT 'scripts\desktop_launcher.py') + '"'
for ($i = 1; ; $i++) {
    $name = if ($i -eq 1) { '天衍系统' } else { "天衍系统 ($i)" }
    $path = Join-Path $desktop ($name + '.lnk')
    if (Test-Path -LiteralPath $path) {
        $existing = $shell.CreateShortcut($path)
        if ($existing.Arguments -ne $arguments -or $existing.TargetPath -ne $env:TIANYAN_LINK_PYTHON) { continue }
    }
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $env:TIANYAN_LINK_PYTHON
    $link.Arguments = $arguments
    $link.WorkingDirectory = $env:TIANYAN_LINK_ROOT
    $link.IconLocation = (Join-Path $env:TIANYAN_LINK_ROOT 'frontend\public\tianyan-brand.ico') + ',0'
    $link.Save()
    break
}
"""
    env = dict(os.environ, TIANYAN_LINK_ROOT=str(root), TIANYAN_LINK_PYTHON=str(python),
               TIANYAN_LINK_DIR=str(directory or ""))
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                   env=env, check=True)


def open_instance():
    port = int(config()["WEB_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("本机端口无效")
    url = f"http://127.0.0.1:{port}"
    if not shutil.which("docker"):
        raise RuntimeError("请先安装并启动 Docker Desktop，使用 Linux 容器。")
    running = compose("ps", "--status", "running", "-q", "web", capture_output=True, text=True)
    if not running.stdout.strip():
        start_instance()
    wait_ready(url)
    ticket = compose("exec", "-T", "api", "python", "-m", "app.cli", "desktop-ticket",
                     capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{64}", ticket):
        raise RuntimeError("无法打开本机工作空间，请检查实例状态。")
    if not webbrowser.open(f"{url}/workbench#desktop_ticket={ticket}"):
        raise RuntimeError("无法启动默认浏览器。")


if __name__ == "__main__":
    try:
        open_instance()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, SystemExit) as exc:
        print(f"工作台未能打开。请确认 Docker Desktop 已启动且使用 Linux 容器。\n{exc}")
        input("按回车退出。")
        sys.exit(1)
