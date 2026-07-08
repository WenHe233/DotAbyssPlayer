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
unitypy_datas, unitypy_bins, unitypy_hidden = collect_all("UnityPy")
sf_datas, sf_bins, sf_hidden = collect_all("soundfile")

datas = [
    (os.path.join(REPO, "src", "AdvPlayer"), "AdvPlayer"),
]
_analysis = os.path.join(REPO, "tools", "novel_command_analysis.json")
if os.path.exists(_analysis):
    datas.append((_analysis, "."))
datas += unitypy_datas + sf_datas

hiddenimports = [
    # tool modules imported lazily (importlib) by the pipeline — pin them in.
    "pipeline", "adv_extract", "convert_wav_audio_to_ogg",
    "extract_bg_assets", "extract_charastand_assets",
    "extract_global_se_assets", "extract_global_bgm_assets",
] + unitypy_hidden + sf_hidden

a = Analysis(
    [os.path.join(REPO, "scripts", "serve_advplayer.py")],
    pathex=[os.path.join(REPO, "tools"), os.path.join(REPO, "scripts")],
    binaries=unitypy_bins + sf_bins,
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
