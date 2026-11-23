# Windows distribution notices

The Windows package contains the same Tianyan source and locked Python dependencies as the server build. No market data, account, API key, private master key or user database belongs in a program package.

| Component | Pinned version | Upstream | Notice location in package |
|---|---|---|---|
| CPython | 3.14.0 x64 embeddable | https://www.python.org/downloads/release/python-3140/ | runtime/LICENSE.txt |
| PostgreSQL binaries | 17.11-5 x64 | https://www.postgresql.org/download/windows/ | postgresql/share and bundled component notices |
| WinSW | 2.12.0 x64 | https://github.com/winsw/winsw/releases/tag/v2.12.0 | licenses/WinSW-LICENSE.txt |
| NSIS (build tool only) | 3.11 | https://nsis.sourceforge.io/ | cache/nsis-3.11/Docs/AppendixI.html |
| Microsoft Visual C++ Redistributable x64 | 14.44.35211.0 | https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist | original signed redist/VC_redist.x64.exe, embedded Microsoft license |

Python package licenses and attribution are carried in each installed `*.dist-info` or source package. Frontend production dependencies React 19.3.1, React DOM 19.3.1 and Scheduler 0.28.0 are MIT licensed; their complete upstream licenses are retained under `licenses/frontend/`. The builder verifies their installed versions against `frontend/package-lock.json` and requires their notices. The complete pinned artifact URLs and SHA256 values are in `assets.lock.json`; Python dependencies and hashes are in `requirements.lock`.

The hash lock provides repeatability and integrity after the initial trusted HTTPS acquisition. It is not a claim that an independent upstream signature or checksum was available for every binary. Maintainers must review third-party notices and the project's own publication license before publishing a release.

The PostgreSQL Windows zip imports the Microsoft C/C++ runtime (`VCRUNTIME140.dll`, `MSVCP140.dll` via ICU). The original Microsoft-signed redistributable is included and installed by NSIS rather than copying DLLs out of the maintainer's Windows installation. It retains Microsoft's original license and signature and is not covered by the project's license.
