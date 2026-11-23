"""Desktop shortcut: open the installed service, never own its process."""
import argparse
import json
from pathlib import Path
import urllib.request
import webbrowser

from app.native.config import NativeConfig


def destination(config, *, opener=urllib.request.urlopen):
    with opener(config.origin + '/native/setup/status', timeout=5) as response:
        value = json.load(response)
    if value.get('setup_required'):
        token = config.private().get('setup_token')
        if not token:
            raise RuntimeError('native_setup_token_missing')
        return config.origin + '/native/setup#' + token
    return config.origin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    try:
        webbrowser.open(destination(NativeConfig.read(args.config)), new=2)
    except Exception:
        # pythonw has no terminal. Show an actionable message without dumping a
        # DSN, private path, one-time token or a provider's response.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, '后台服务尚未就绪。请稍候重试；持续无法打开时，可重新运行安装包检查并修复后台服务。',
                                        '天衍系统', 0x10)


if __name__ == '__main__':
    main()
