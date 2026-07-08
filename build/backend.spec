# PyInstaller spec for the DotAbyss desktop backend sidecar.
# Build:  pyinstaller build/backend.spec  (run from repo root)
# Output: dist/DotAbyssBackend/  (onedir) -> copied into the Tauri bundle by build.ps1
#
# The backend bundles the static player + all extraction tooling so the frozen exe is
# self-sufficient (no Python/UnityPy/soundfile install on the user's machine).
import os
from PyInstaller.utils.hooks import collect_all

REPO = os.path.abspath(os.getcwd())

# UnityPy + soundfile ship native libs / data files that must be collected.
# archspec (pulled transitively via UnityPy -> etcpak for CPU detection) ships JSON data
# files under archspec/json/ that collect_all("UnityPy") does NOT pick up — without them the
# frozen exe crashes at import time (FileNotFoundError microarchitectures.json). Collect them.
_datas, _bins, _hidden = [], [], []
for pkg in ("UnityPy", "soundfile", "archspec", "etcpak"):
    try:
        d, b, h = collect_all(pkg)
        _datas += d
        _bins += b
        _hidden += h
    except Exception:
        pass

datas = [
    (os.path.join(REPO, "src", "AdvPlayer"), "AdvPlayer"),
]
_analysis = os.path.join(REPO, "tools", "novel_command_analysis.json")
if os.path.exists(_analysis):
    datas.append((_analysis, "."))
datas += _datas

# Belt-and-suspenders: force archspec's JSON cpu database in (the exact files the
# import-time crash needed at _internal/archspec/json/cpu/microarchitectures.json).
try:
    import archspec  # noqa: E402
    _arch_json = os.path.join(os.path.dirname(archspec.__file__), "json")
    if os.path.isdir(_arch_json):
        datas.append((_arch_json, os.path.join("archspec", "json")))
except Exception:
    pass

hiddenimports = [
    # tool modules imported lazily (importlib) by the pipeline — pin them in.
    "pipeline", "adv_extract", "convert_wav_audio_to_ogg",
    "extract_bg_assets", "extract_charastand_assets",
    "extract_global_se_assets", "extract_global_bgm_assets",
] + _hidden

a = Analysis(
    [os.path.join(REPO, "scripts", "serve_advplayer.py")],
    pathex=[os.path.join(REPO, "tools"), os.path.join(REPO, "scripts")],
    binaries=_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="DotAbyssBackend",
    console=True,           # keep a console for logs; the Tauri shell spawns it hidden
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="DotAbyssBackend",
)
