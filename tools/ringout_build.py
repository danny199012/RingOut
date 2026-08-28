#!/usr/bin/env python3
"""
RingOut Windows Builder
=======================
Automates the full native build of the SoulCalibur II static recompilation
(RingOut) on Windows, with a GUI.

What this script does:
  1. Checks / installs all required build tools (Git, CMake, Ninja, clang
     via llvm-mingw, Python 3, Vulkan SDK hint, MSVC via VS Build Tools).
  2. Fetches the RingOut source (clone from GitHub, or use an existing
     checkout the user points at).
  3. Builds the ModernGekko runtime  ->  moderngekko-run.exe  (MSVC + VS gen)
  4. Builds the DolRecomp recompiler ->  dolrecomp.exe        (clang + Ninja)
  5. Compiles the Windows launcher   ->  RingOut.exe         (clang)
  6. Assembles everything under  RingOut-windows  (or --out DIR).

The game disc is NOT handled here: RingOut's launcher extracts and recompiles
the disc on first run, so there is no --iso path.

Requirements (installed automatically where possible):
  - Windows 10 / 11, x86-64
  - Internet access on first run (to download tools)
  - ~6 GB free disk space
  - A Vulkan-capable GPU with up-to-date drivers

Usage:
  python ringout_build.py                 # launch the GUI
  python ringout_build.py --headless build # run one build with no GUI
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Release / download coordinates
# ---------------------------------------------------------------------------
# Point at the user's fork so the dual-controller fix is included automatically.
REPO_URL      = "https://github.com/danny199012/RingOut.git"
REPO_DIR_NAME = "RingOut-src"

# llvm-mingw: self-contained clang + lld + ucrt headers for Windows
LLVM_MINGW_RELEASE_API   = "https://api.github.com/repos/mstorsjo/llvm-mingw/releases/latest"
LLVM_MINGW_ASSET_PATTERN = re.compile(r"llvm-mingw-\d+-ucrt-x86_64\.zip", re.IGNORECASE)

# Regex to strip ANSI colour codes from log file output
ANSI_RE = re.compile(r'\033\[[0-9;]*m')

# CMake portable zip (fallback if cmake not on PATH)
CMAKE_DOWNLOAD_URL = (
    "https://github.com/Kitware/CMake/releases/download/"
    "v3.29.6/cmake-3.29.6-windows-x86_64.zip"
)

# Ninja (fallback)
NINJA_RELEASE_API = "https://api.github.com/repos/ninja-build/ninja/releases/latest"

# Python embeddable (bundled inside the output package for module builds)
PYTHON_EMBED_VER = "3.11.9"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_EMBED_VER}/"
    f"python-{PYTHON_EMBED_VER}-embed-amd64.zip"
)

# ---------------------------------------------------------------------------
# Console colours
# ---------------------------------------------------------------------------
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

def _enable_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_ansi()

def log(msg, colour=RESET): print(f"{colour}{msg}{RESET}", flush=True)
def step(title):
    log(f"\n{'='*60}", CYAN)
    log(f"  {title}", BOLD)
    log(f"{'='*60}", CYAN)
def ok(msg):    log(f"  [OK]  {msg}", GREEN)
def warn(msg):  log(f"  [!!]  {msg}", YELLOW)
def info(msg):  log(f"  [..]  {msg}")
def error(msg): log(f"  [ERR] {msg}", RED)
def die(msg):   error(msg); sys.exit(1)

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def run(cmd, *, cwd=None, env=None, check=True, capture=False, quiet=False):
    """Run a subprocess, streaming output unless capture=True."""
    if not quiet:
        shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        info("Running: " + shown)
    kwargs = dict(cwd=cwd, env=env)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"]   = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        die(f"Command failed (exit {result.returncode})")
    return result

def which(name, extra_paths=None):
    """Find an executable on PATH (and optional extra dirs)."""
    dirs = list(os.environ.get("PATH", "").split(os.pathsep))
    if extra_paths:
        dirs = list(extra_paths) + dirs
    exts = [""] if sys.platform != "win32" else [".exe", ".cmd", ".bat", ""]
    for d in dirs:
        if not d:
            continue
        for ext in exts:
            p = Path(d) / (name + ext)
            if p.is_file():
                return str(p)
    return None

def download(url, dest_path, label=None):
    """Download a URL to a file with a simple progress indicator."""
    label = label or Path(dest_path).name
    info(f"Downloading {label} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ringout-builder/1.0"})
        with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length", 0))
            done  = 0
            chunk = 1 << 16
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                fh.write(data)
                done += len(data)
                if total:
                    pct = done * 100 // total
                    print(f"\r    {pct:3d}% ({done//(1<<20)} MB / {total//(1<<20)} MB)",
                          end="", flush=True)
        print()
        ok(f"Downloaded {label}")
    except Exception as exc:
        die(f"Download failed for {url}: {exc}")

def extract_zip(zip_path, dest_dir, strip_components=0):
    """Extract a zip, optionally stripping top-level directory components."""
    info(f"Extracting {Path(zip_path).name} -> {dest_dir} ...")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            parts = Path(member).parts
            if strip_components and len(parts) <= strip_components:
                continue
            rel = Path(*parts[strip_components:]) if strip_components else Path(member)
            target = Path(dest_dir) / rel
            if member.endswith("/") or member.endswith("\\"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    ok(f"Extracted to {dest_dir}")

def github_latest_asset(api_url, pattern):
    """Return (name, browser_download_url) for the first asset matching pattern."""
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "ringout-builder/1.0",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    for asset in data.get("assets", []):
        if re.search(pattern, asset["name"]):
            return asset["name"], asset["browser_download_url"]
    raise RuntimeError(f"No asset matching {pattern!r} in {api_url}")

def ensure_dir(p):
    Path(p).mkdir(parents=True, exist_ok=True)
    return Path(p)

def rmtree(p):
    p = Path(p)
    if p.exists():
        shutil.rmtree(p, onerror=lambda f, path, e: (os.chmod(path, stat.S_IWRITE), f(path)))

# ---------------------------------------------------------------------------
# Tool tracking
# ---------------------------------------------------------------------------

class ToolSet:
    """Tracks paths to every required tool."""
    def __init__(self, tools_dir: Path):
        self.tools_dir = tools_dir
        self.cmake   = None
        self.ninja   = None
        self.clang   = None
        self.clangxx = None
        self.git     = None
        self.python  = None
        self.msvc_found    = False
        self.vs_generator  = None  # e.g. "Visual Studio 17 2022"

    def env(self, extra_path_dirs=None):
        """Build an os.environ copy that has all known tools on PATH."""
        e = os.environ.copy()
        prepend = []
        for attr in ("cmake", "ninja", "clang", "git", "python"):
            val = getattr(self, attr)
            if val:
                d = str(Path(val).parent)
                if d not in prepend:
                    prepend.append(d)
        if extra_path_dirs:
            prepend.extend(str(p) for p in extra_path_dirs)
        if prepend:
            e["PATH"] = os.pathsep.join(prepend) + os.pathsep + e.get("PATH", "")
        return e

# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------

def ensure_git(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("git")
    if path:
        tools.git = path
        ok(f"git: {path}")
        return
    if skip:
        die("git not found on PATH. Install Git for Windows: https://git-scm.com/downloads/win")
    warn("git not found - downloading portable MinGit ...")
    minigit_api = "https://api.github.com/repos/git-for-windows/git/releases/latest"
    try:
        name, url = github_latest_asset(minigit_api, r"MinGit-.*-64-bit\.zip")
    except Exception as exc:
        die(f"Could not find a MinGit release: {exc}")
    dest = dl_dir / "mingit.zip"
    download(url, dest, label=name)
    git_dir = tools.tools_dir / "mingit"
    extract_zip(dest, git_dir, strip_components=0)
    git_exe = git_dir / "cmd" / "git.exe"
    if not git_exe.exists():
        die(f"git.exe not found after extracting MinGit to {git_dir}")
    tools.git = str(git_exe)
    ok(f"git: {tools.git}")

def ensure_cmake(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("cmake")
    if path:
        tools.cmake = path
        ok(f"cmake: {path}")
        return
    if skip:
        die("cmake not found. Download from https://cmake.org/download/")
    warn("cmake not found - downloading portable CMake ...")
    dest = dl_dir / "cmake.zip"
    download(CMAKE_DOWNLOAD_URL, dest, label="cmake-3.29.6-windows-x86_64.zip")
    cmake_dir = tools.tools_dir / "cmake"
    extract_zip(dest, cmake_dir, strip_components=1)
    cmake_exe = cmake_dir / "bin" / "cmake.exe"
    if not cmake_exe.exists():
        die(f"cmake.exe not found after extraction at {cmake_dir}")
    tools.cmake = str(cmake_exe)
    ok(f"cmake: {tools.cmake}")

def ensure_ninja(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("ninja")
    if path:
        tools.ninja = path
        ok(f"ninja: {path}")
        return
    if skip:
        die("ninja not found. Download from https://github.com/ninja-build/ninja/releases")
    warn("ninja not found - downloading latest ...")
    try:
        name, url = github_latest_asset(NINJA_RELEASE_API, r"ninja-win\.zip")
    except Exception as exc:
        die(f"Could not find a Ninja release: {exc}")
    dest = dl_dir / "ninja.zip"
    download(url, dest, label=name)
    ninja_dir = tools.tools_dir / "ninja"
    ensure_dir(ninja_dir)
    extract_zip(dest, ninja_dir)
    ninja_exe = ninja_dir / "ninja.exe"
    if not ninja_exe.exists():
        die("ninja.exe not found after extraction")
    tools.ninja = str(ninja_exe)
    ok(f"ninja: {tools.ninja}")

def ensure_llvm_mingw(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("clang")
    if path:
        tools.clang   = path
        tools.clangxx = which("clang++") or path
        ok(f"clang: {path}")
        return
    if skip:
        die("clang not found on PATH. Install LLVM or llvm-mingw.")
    warn("clang not found - downloading llvm-mingw (clang + C runtime) ...")
    try:
        name, url = github_latest_asset(LLVM_MINGW_RELEASE_API, LLVM_MINGW_ASSET_PATTERN)
    except Exception as exc:
        die(f"Could not find an llvm-mingw release: {exc}")
    dest = dl_dir / "llvm-mingw.zip"
    download(url, dest, label=name)
    llvm_dir = tools.tools_dir / "llvm-mingw"
    extract_zip(dest, llvm_dir, strip_components=1)
    clang_exe = llvm_dir / "bin" / "clang.exe"
    if not clang_exe.exists():
        die(f"clang.exe not found after llvm-mingw extraction at {llvm_dir}")
    tools.clang   = str(clang_exe)
    tools.clangxx = str(llvm_dir / "bin" / "clang++.exe")
    ok(f"clang: {tools.clang}")

def ensure_python(tools: ToolSet, dl_dir: Path):
    """Find Python 3 on PATH; if absent download the embedded interpreter."""
    path = which("python3") or which("python")
    if path:
        r = subprocess.run([path, "--version"], capture_output=True, text=True)
        if "Python 3" in (r.stdout + r.stderr):
            tools.python = path
            ok(f"python: {path}")
            return
    warn("Python 3 not found on PATH - downloading embedded interpreter ...")
    dest = dl_dir / "python-embed.zip"
    download(PYTHON_EMBED_URL, dest, label=f"python-{PYTHON_EMBED_VER}-embed-amd64.zip")
    py_dir = tools.tools_dir / "python"
    ensure_dir(py_dir)
    extract_zip(dest, py_dir)
    py_exe = py_dir / "python.exe"
    if not py_exe.exists():
        die(f"python.exe not found after extraction at {py_dir}")
    tools.python = str(py_exe)
    ok(f"python: {tools.python}")

def check_vulkan():
    """Warn if Vulkan SDK is absent (build works, runtime needs a Vulkan driver)."""
    sdk = os.environ.get("VULKAN_SDK", "")
    if sdk and Path(sdk).exists():
        ok(f"Vulkan SDK: {sdk}")
        return
    for candidate in [Path(r"C:\VulkanSDK"),
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VulkanSDK"]:
        if candidate.exists():
            subdirs = sorted(candidate.iterdir())
            if subdirs:
                sdk = str(subdirs[-1])
                os.environ["VULKAN_SDK"] = sdk
                ok(f"Vulkan SDK (auto-detected): {sdk}")
                return
    warn(
        "Vulkan SDK not found. The runtime needs a Vulkan GPU driver at runtime.\n"
        "  Make sure your GPU drivers include Vulkan support.\n"
        "  Optional SDK for development: https://vulkan.lunarg.com/sdk/home#windows"
    )

# ---------------------------------------------------------------------------
# MSVC detection (the Dolphin runtime MUST be built with MSVC on Windows)
# ---------------------------------------------------------------------------

def ensure_msvc(tools: ToolSet, dl_dir: Path, skip: bool):
    """Detect Visual Studio / MSVC, preferring VS 2022 (version 17).
    The Dolphin runtime MUST be built with MSVC on Windows.
    VS 2022 is preferred. VS 2026+ may have C2440 errors.
    """
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    def _vswhere_find(version_range):
        if not vswhere.exists(): return None, None
        r = subprocess.run([str(vswhere), "-latest", "-products", "*", "-version", version_range, "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip(): return None, None
        vs_path = r.stdout.strip().splitlines()[0].strip()
        r2 = subprocess.run([str(vswhere), "-latest", "-products", "*", "-version", version_range, "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationVersion"], capture_output=True, text=True)
        ver_str = r2.stdout.strip() if r2.returncode == 0 else ""
        major = int(ver_str.split('.')[0]) if ver_str.split('.')[0].isdigit() else 17
        return vs_path, major
    def _set_generator(major):
        if major == 17: tools.vs_generator = "Visual Studio 17 2022"
        elif major == 16: tools.vs_generator = "Visual Studio 16 2019"
        else: tools.vs_generator = f"Visual Studio {major}"
        tools.msvc_found = True
    vs_path, major = _vswhere_find("[17.0,18.0)")
    if vs_path:
        _set_generator(major)
        ok(f"Visual Studio 2022 found: {vs_path}")
        ok(f"Using generator: {tools.vs_generator}")
        return
    if skip:
        vs_path, major = _vswhere_find("[18.0,)")
        if vs_path:
            _set_generator(major)
            warn(f"Only Visual Studio {major} was found (not VS 2022).")
            return
    cl = which("cl")
    if cl:
        ok(f"MSVC (cl.exe) found on PATH: {cl}")
        tools.msvc_found = True
        tools.vs_generator = "Visual Studio 17 2022"
        return
    if skip:
        warn("MSVC not found. Install VS 2022 Build Tools.")
        return
    warn("VS 2022 not found - downloading Visual Studio 2022 Build Tools ...")
    url = "https://aka.ms/vs/17/release/vs_buildtools.exe"
    dest = dl_dir / "vs_buildtools.exe"
    download(url, dest, label="vs_buildtools.exe")
    info("Installing Visual Studio 2022 Build Tools (C++ workload) ...")
    result = subprocess.run([str(dest), "--quiet", "--wait", "--norestart", "--add", "Microsoft.VisualStudio.Workload.VCTools", "--includeRecommended"], timeout=2400)
    if result.returncode != 0:
        warn(f"VS Build Tools installer returned code {result.returncode}")
        return
    ok("Visual Studio 2022 Build Tools installed.")
    vs_path, major = _vswhere_find("[17.0,18.0)")
    if vs_path:
        _set_generator(major)
        ok(f"Using generator: {tools.vs_generator}")

# ---------------------------------------------------------------------------
# Source checkout
# ---------------------------------------------------------------------------

def clone_or_update_repo(repo_dir: Path, tools: ToolSet):
    env = tools.env()
    run([tools.git, "config", "--global", "core.longpaths", "true"], env=env, quiet=True)
    if (repo_dir / ".git").exists():
        ok(f"Repository already exists at {repo_dir} - pulling latest ...")
        run([tools.git, "-C", str(repo_dir), "pull", "--ff-only"], env=env)
    else:
        info(f"Cloning {REPO_URL} into {repo_dir} ...")
        run([tools.git, "clone", "--depth=1", "-c", "core.longpaths=true", REPO_URL, str(repo_dir)], env=env)
    ok("Repository ready.")

# ---------------------------------------------------------------------------
# CMake configure / build helpers
# ---------------------------------------------------------------------------

def cmake_configure(src, build, generator, extra_defs, tools, rebuild):
    if rebuild: rmtree(build)
    ensure_dir(build)
    env = tools.env()
    cmd = [tools.cmake, "-S", str(src), "-B", str(build), "-G", generator, f"-DCMAKE_MAKE_PROGRAM={tools.ninja}", f"-DCMAKE_C_COMPILER={tools.clang}", f"-DCMAKE_CXX_COMPILER={tools.clangxx}", "-DCMAKE_BUILD_TYPE=Release"]
    for k, v in extra_defs.items(): cmd.append(f"-D{k}={v}")
    run(cmd, env=env)

def cmake_configure_msvc(src, build, vs_generator, extra_defs, tools, rebuild):
    if rebuild: rmtree(build)
    ensure_dir(build)
    env = tools.env()
    cmd = [tools.cmake, "-S", str(src), "-B", str(build), "-G", vs_generator, "-A", "x64"]
    for k, v in extra_defs.items(): cmd.append(f"-D{k}={v}")
    run(cmd, env=env)

def cmake_build(build, target, tools, jobs):
    env = tools.env()
    cmd = [tools.cmake, "--build", str(build), "--config", "Release", "--target", target, f"-j{jobs}"]
    run(cmd, env=env)

# ---------------------------------------------------------------------------
# Build stages
# ---------------------------------------------------------------------------

def build_moderngekko(repo, build_root, tools, rebuild, jobs):
    step("Building ModernGekko runtime (moderngekko-run.exe)")
    src = repo / "ModernGekko"
    build = build_root / "moderngekko-build"
    defs = {"MODERNGEKKO_ENABLE_DOLPHIN_RUNTIME": "ON", "ENABLE_QT": "OFF", "ENABLE_NOGUI": "OFF", "ENABLE_TESTS": "OFF", "ENABLE_ANALYTICS": "OFF", "ENABLE_AUTOUPDATE": "OFF", "USE_RETRO_ACHIEVEMENTS": "OFF", "USE_DISCORD_PRESENCE": "OFF", "USE_MGBA": "OFF", "USE_UPNP": "OFF", "ENCODE_FRAMEDUMPS": "OFF", "ENABLE_LLVM": "OFF", "CMAKE_CXX_FLAGS": "/Zc:preprocessor /wd4067 /wd4804 /wd4805", "CMAKE_C_FLAGS": "/Zc:preprocessor", "CMAKE_CXX_STANDARD": "20"}
    if tools.msvc_found and tools.vs_generator:
        info("Using MSVC (Visual Studio generator) for the Dolphin runtime")
        cmake_configure_msvc(src, build, tools.vs_generator, defs, tools, rebuild)
    else:
        die("MSVC is required to build the Dolphin runtime on Windows.")
    cmake_build(build, "moderngekko-run", tools, jobs)
    exe = None
    for candidate in [build / "moderngekko-run.exe", build / "Source" / "Core" / "moderngekko-run.exe", build / "Binaries" / "moderngekko-run.exe"]:
        if candidate.exists(): exe = candidate; break
    if exe is None:
        hits = list(build.rglob("moderngekko-run.exe"))
        if hits: exe = hits[0]
        else: die("moderngekko-run.exe not found after build")
    ok(f"Runtime built: {exe}")
    return exe

def build_dolrecomp(repo, build_root, tools, rebuild, jobs):
    step("Building DolRecomp recompiler (dolrecomp.exe)")
    src = repo / "DolRecomp"
    build = build_root / "dolrecomp-build"
    defs = {"DOLRECOMP_ENABLE_LLVM": "OFF"}
    info("Using llvm-mingw clang + Ninja for DolRecomp (provides POSIX headers)")
    cmake_configure(src, build, "Ninja", defs, tools, rebuild)
    cmake_build(build, "dolrecomp", tools, jobs)
    exe = None
    for candidate in [build / "dolrecomp.exe", build / "Release" / "dolrecomp.exe"]:
        if candidate.exists(): exe = candidate; break
    if exe is None:
        hits = list(build.rglob("dolrecomp.exe"))
        if hits: exe = hits[0]
        else: die("dolrecomp.exe not found after build")
    ok(f"Recompiler built: {exe}")
    return exe

def build_launcher(repo, build_root, tools):
    step("Building RingOut.exe launcher")
    src_c = (repo / "attic" / "windows" / "dist" / "RingOut-1.0-dist-windows" / "launcher" / "RingOut.c")
    if not src_c.exists():
        warn(f"Launcher source not found at {src_c}. Skipping RingOut.exe.")
        return None
    out_exe = build_root / "RingOut.exe"
    env = tools.env()
    cmd = [tools.clang, str(src_c), "-o", str(out_exe), "-municode", "-O2", "-lcomdlg32"]
    run(cmd, env=env)
    if not out_exe.exists(): die("RingOut.exe was not produced by the launcher build.")
    ok(f"Launcher built: {out_exe}")
    return out_exe

# ---------------------------------------------------------------------------
# Package assembly
# ---------------------------------------------------------------------------

def _copy_tree(src: Path, dst: Path):
    """Copy a directory tree into dst (merging), preserving attributes."""
    ensure_dir(dst)
    for item in src.iterdir():
        s = item
        d = dst / item.name
        if s.is_dir():
            _copy_tree(s, d)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)

def _bundle_toolchain(tools: ToolSet, out_dir: Path):
    """Bundle clang/lld + cmake + ninja + python into out_dir/toolchain so the
    first-run module build needs nothing installed. Reuses the tool archives
    already downloaded/extracted by the ensure_* functions."""
    step("Bundling first-run toolchain (clang, lld, cmake, ninja, python)")
    tc = out_dir / "toolchain"
    if tc.exists(): rmtree(tc)
    ensure_dir(tc / "bin")
    ensure_dir(tc / "python")

    # clang + lld come from llvm-mingw (tools.clang points at .../bin/clang.exe)
    if tools.clang:
        llvm_bin = Path(tools.clang).parent
        llvm_root = llvm_bin.parent
        for entry in llvm_root.iterdir():
            name = entry.name
            # Drop unrelated target sysroots + upstream's own python to stay small.
            if name in ("aarch64-w64-mingw32", "arm64ec-w64-mingw32",
                        "armv7-w64-mingw32", "i686-w64-mingw32", "python"):
                continue
            dest = tc / name
            if entry.is_dir():
                _copy_tree(entry, dest)
            else:
                shutil.copy2(entry, dest)
    else:
        warn("clang not available - toolchain will lack the compiler.")

    # cmake (portable) - copy bin/ and share/ next to the bundled tools
    if tools.cmake:
        cmake_bin = Path(tools.cmake).parent
        for exe in cmake_bin.glob("cmake*.exe"):
            shutil.copy2(exe, tc / "bin" / exe.name)
        share = cmake_bin.parent / "share"
        if share.exists():
            _copy_tree(share, tc / "share")

    # ninja
    if tools.ninja:
        shutil.copy2(tools.ninja, tc / "bin" / Path(tools.ninja).name)

    # python (embedded) for the module build scripts
    if tools.python:
        py_bin = Path(tools.python).parent
        for item in py_bin.iterdir():
            d = tc / "python" / item.name
            if item.is_dir():
                _copy_tree(item, d)
            else:
                shutil.copy2(item, d)

    # Sanity-check the bundle the same way the release pipeline does.
    for marker in ["bin/clang.exe", "bin/cmake.exe", "bin/ninja.exe", "python/python.exe"]:
        if not (tc / marker).exists():
            warn(f"toolchain marker missing: {marker}")
    has_lld = (tc / "bin" / "ld.lld.exe").exists() or (tc / "bin" / "lld-link.exe").exists()
    if not has_lld:
        warn("toolchain has no LLVM linker (ld.lld.exe / lld-link.exe)")
    ok(f"Toolchain bundled into {tc}")

def assemble_package(repo, build_root, out_dir, runtime_exe, recomp_exe, launcher_exe, tools):
    """Assemble the runnable Windows package under out_dir."""
    step(f"Assembling package into {out_dir}")
    if out_dir.exists(): rmtree(out_dir)
    ensure_dir(out_dir)
    ensure_dir(out_dir / "bin")
    ensure_dir(out_dir / "tools")

    # Launcher at the top level (double-click to play).
    if launcher_exe:
        shutil.copy2(launcher_exe, out_dir / "RingOut.exe")
        ok(f"RingOut.exe -> {out_dir / 'RingOut.exe'}")

    # Runtime under bin/, recompiler under tools/.
    shutil.copy2(runtime_exe, out_dir / "bin" / "moderngekko-run.exe")
    ok(f"moderngekko-run.exe -> {out_dir / 'bin'}")
    shutil.copy2(recomp_exe, out_dir / "tools" / "dolrecomp.exe")
    ok(f"dolrecomp.exe -> {out_dir / 'tools'}")

    # Windows support scripts / docs from the repo's dist tree.
    win_dist = repo / "dist" / "windows"
    for name in ["RingOut.cmd", "RingOut.ps1", "setup.ps1", "README.txt",
                 "CREDITS.txt", "THIRD-PARTY-NOTICES.txt"]:
        src = win_dist / name
        if src.exists():
            shutil.copy2(src, out_dir / name)

    # Module sources + shaders (the first-run build compiles the user's disc).
    module_src = repo / "dist" / "RingOut-1.0-dist" / "module-src"
    if module_src.exists():
        _copy_tree(module_src, out_dir / "module-src")
        prof = out_dir / "module-src" / "module.profdata"
        if prof.exists(): prof.unlink()  # private profile, not consumed on Windows
        ok("module-src staged")
    else:
        warn("module-src not found in repo; first-run build will need sources.")

    shaders = repo / "dist" / "RingOut-1.0-dist" / "shaders"
    if shaders.exists():
        _copy_tree(shaders, out_dir / "shaders")
        ok("shaders staged")

    # Tracked game settings (cheat codes stripped, same as the release pipeline).
    gs = repo / "work" / "mg_userdir" / "GameSettings" / "GRSEAF.ini"
    if gs.exists():
        ensure_dir(out_dir / "userdata" / "GameSettings")
        enabled = False
        out_lines = []
        for line in gs.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() in ("[ActionReplay_Enabled]", "[Gecko_Enabled]"):
                enabled = True; out_lines.append(line); continue
            if line.startswith("[") and line.endswith("]"):
                enabled = False
            if enabled and line.strip() == "":
                continue
            out_lines.append(line)
        (out_dir / "userdata" / "GameSettings" / "GRSEAF.ini").write_text(
            "\n".join(out_lines) + "\n", encoding="utf-8")
        ok("game settings staged (cheats stripped)")
    else:
        warn("tracked GRSEAF.ini not found; skipping game settings.")

    # Bundled toolchain so the first-run module build needs nothing installed.
    _bundle_toolchain(tools, out_dir)

    ok(f"Package assembled: {out_dir}")
    return out_dir

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(out_dir):
    step("Build complete!")
    exe = out_dir / "RingOut.exe"
    cmd = out_dir / "RingOut.cmd"
    launch = "Double-click RingOut.exe" if exe.exists() else ("Run RingOut.cmd" if cmd.exists() else "(check out_dir)")
    log(textwrap.dedent(f"""
    +---------------------------------------------------------+
    |              RingOut Windows build finished              |
    +---------------------------------------------------------+
    Output folder: {out_dir}
    To play: {launch}
    """), GREEN)
    log("Controls (in-game):", CYAN)
    log("  Escape = Settings menu | Alt+Enter = fullscreen | F1-F8 = load state")
    log("  Shift+F1-F8 = save state | Alt+W = widescreen toggle\n")

# ---------------------------------------------------------------------------
# Build orchestration (runs on a worker thread; logs go to log_queue)
# ---------------------------------------------------------------------------

def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()

def do_build(source_dir: Optional[Path], out_dir: Optional[Path],
             skip_deps: bool, rebuild: bool, no_launcher: bool, jobs: int,
             log_queue: "queue.Queue", stop_event: "threading.Event") -> bool:
    """Run the full build. Returns True on success, False on failure.
    All log output is redirected into log_queue so the GUI/headless tee can
    consume it. Checks stop_event between major steps."""
    global log, ok, warn, info, error, step
    # --- redirect the module-level log helpers into the queue ---
    originals = (log, ok, warn, info, error, step)
    def _q_log(msg, colour=RESET):
        log_queue.put(("log", str(msg), colour))
    def _q_ok(msg):    log_queue.put(("ok", str(msg), GREEN))
    def _q_warn(msg):  log_queue.put(("warn", str(msg), YELLOW))
    def _q_info(msg):  log_queue.put(("info", str(msg), RESET))
    def _q_error(msg): log_queue.put(("error", str(msg), RED))
    def _q_step(title):
        log_queue.put(("step", str(title), CYAN))
    log, ok, warn, info, error, step = _q_log, _q_ok, _q_warn, _q_info, _q_error, _q_step

    try:
        work_dir = Path.cwd()
        tools_dir = work_dir / "ringout-tools"
        dl_dir = tools_dir / "downloads"
        build_root = work_dir / "ringout-build"
        ensure_dir(tools_dir)
        ensure_dir(dl_dir)
        ensure_dir(build_root)

        tools = ToolSet(tools_dir)

        # git is needed to clone, so resolve it first.
        ensure_git(tools, dl_dir, skip_deps)

        # --- source: use an existing checkout, or clone from GitHub ---
        if source_dir and (source_dir / "ModernGekko" / "CMakeLists.txt").exists():
            repo_dir = Path(source_dir)
            ok(f"Using existing source checkout: {repo_dir}")
        else:
            repo_dir = work_dir / REPO_DIR_NAME
            if _stopped(stop_event): warn("Build stopped before cloning."); return False
            clone_or_update_repo(repo_dir, tools)

        # --- remaining dependency checks ---
        if _stopped(stop_event): warn("Build stopped."); return False
        ensure_cmake(tools, dl_dir, skip_deps)
        ensure_ninja(tools, dl_dir, skip_deps)
        ensure_llvm_mingw(tools, dl_dir, skip_deps)
        ensure_python(tools, dl_dir)
        check_vulkan()
        ensure_msvc(tools, dl_dir, skip_deps)

        # --- build stages ---
        if _stopped(stop_event): warn("Build stopped before compiling."); return False
        runtime_exe = build_moderngekko(repo_dir, build_root, tools, rebuild, jobs)
        if _stopped(stop_event): warn("Build stopped."); return False
        recomp_exe = build_dolrecomp(repo_dir, build_root, tools, rebuild, jobs)
        launcher_exe = None
        if not no_launcher:
            if _stopped(stop_event): warn("Build stopped before launcher."); return False
            launcher_exe = build_launcher(repo_dir, build_root, tools)

        # --- assemble + summarize ---
        if _stopped(stop_event): warn("Build stopped before packaging."); return False
        final_out = Path(out_dir) if out_dir else (work_dir / "RingOut-windows")
        assemble_package(repo_dir, build_root, final_out, runtime_exe, recomp_exe, launcher_exe, tools)
        print_summary(final_out)
        log_queue.put(("done", str(final_out), GREEN))
        return True
    except SystemExit as exc:
        # die() raises SystemExit(1); surface it as a failure, not a crash.
        error(f"Build aborted: {exc}")
        log_queue.put(("failed", str(exc), RED))
        return False
    except Exception as exc:
        error(f"Build failed: {exc}")
        log_queue.put(("failed", str(exc), RED))
        return False
    finally:
        log, ok, warn, info, error, step = originals

# ---------------------------------------------------------------------------
# GUI (Tkinter, imported lazily so --headless works without it)
# ---------------------------------------------------------------------------

def _run_build_thread(source_dir, out_dir, skip_deps, rebuild, no_launcher, jobs,
                      log_queue, stop_event, result_holder):
    try:
        result_holder["ok"] = do_build(source_dir, out_dir, skip_deps, rebuild,
                                       no_launcher, jobs, log_queue, stop_event)
    except Exception as exc:  # defensive: never crash the worker thread
        log_queue.put(("error", f"Worker thread crashed: {exc}", RED))
        log_queue.put(("failed", str(exc), RED))
        result_holder["ok"] = False

class BuildApp:
    """Tkinter GUI: pick a source root (or clone from GitHub), pick an output
    folder, and build. The build runs on a worker thread; logs stream in."""

    def __init__(self, tk, ttk):
        self.tk = tk
        self.ttk = ttk
        self.log_queue: "queue.Queue" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.result_holder = {"ok": False}
        self.root = tk.Tk()
        self.root.title("RingOut Windows Builder")
        self.root.geometry("820x620")
        self.root.minsize(640, 480)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Source root:").grid(row=0, column=0, sticky="w", **pad)
        self.source_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.source_var).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=self._browse_source).grid(row=0, column=3, **pad)

        self.clone_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Clone from GitHub (empty source clones danny199012/RingOut)",
                        variable=self.clone_var,
                        command=self._on_clone_toggle).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

        ttk.Label(frm, text="Output folder:").grid(row=2, column=0, sticky="w", **pad)
        self.out_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.out_var).grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(frm, text="Browse...", command=self._browse_out).grid(row=2, column=3, **pad)

        ttk.Label(frm, text="Jobs:").grid(row=3, column=0, sticky="w", **pad)
        self.jobs_var = tk.StringVar(value=str(max(1, (os.cpu_count() or 4))))
        ttk.Entry(frm, textvariable=self.jobs_var, width=6).grid(row=3, column=1, sticky="w", **pad)
        self.rebuild_var = tk.BooleanVar(value=False)
        self.no_launcher_var = tk.BooleanVar(value=False)
        self.skip_deps_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Clean rebuild", variable=self.rebuild_var).grid(row=3, column=2, sticky="w", **pad)
        ttk.Checkbutton(frm, text="No launcher", variable=self.no_launcher_var).grid(row=4, column=1, sticky="w", **pad)
        ttk.Checkbutton(frm, text="Skip dep install", variable=self.skip_deps_var).grid(row=4, column=2, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=4, sticky="ew", **pad)
        self.build_btn = ttk.Button(btns, text="Build", command=self._start_build)
        self.build_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Clear log", command=self._clear_log).pack(side="left", padx=4)

        self.log_text = tk.Text(frm, wrap="word", bg="#1e1e1e", fg="#d4d4d4",
                                insertbackground="#d4d4d4", font=("Consolas", 10))
        self.log_text.grid(row=6, column=0, columnspan=4, sticky="nsew", **pad)
        log_scroll = ttk.Scrollbar(frm, command=self.log_text.yview)
        log_scroll.grid(row=6, column=4, sticky="ns")
        self.log_text.config(yscrollcommand=log_scroll.set)
        self._log_tags()

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)
        frm.rowconfigure(6, weight=1)

    def _log_tags(self):
        self.log_text.tag_config("ok", foreground="#6a9955")
        self.log_text.tag_config("warn", foreground="#d7ba7d")
        self.log_text.tag_config("error", foreground="#f44747")
        self.log_text.tag_config("step", foreground="#569cd6", font=("Consolas", 10, "bold"))
        self.log_text.tag_config("info", foreground="#d4d4d4")

    # -- callbacks ----------------------------------------------------------
    def _on_clone_toggle(self):
        if self.source_var.get().strip():
            self.clone_var.set(False)
        else:
            self.clone_var.set(True)

    def _browse_source(self):
        d = self.tk.filedialog.askdirectory(title="Select RingOut source root")
        if d:
            self.source_var.set(d)
            self.clone_var.set(False)

    def _browse_out(self):
        d = self.tk.filedialog.askdirectory(title="Select output folder")
        if d:
            self.out_var.set(d)

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _set_status(self, msg):
        self.status_var.set(msg)

    def _start_build(self):
        if self.worker and self.worker.is_alive():
            self._set_status("A build is already running.")
            return
        src = self.source_var.get().strip()
        source_dir = Path(src) if src else None
        out = self.out_var.get().strip()
        out_dir = Path(out) if out else None
        try:
            jobs = max(1, int(self.jobs_var.get()))
        except ValueError:
            jobs = max(1, (os.cpu_count() or 4))
        skip_deps = bool(self.skip_deps_var.get())
        rebuild = bool(self.rebuild_var.get())
        no_launcher = bool(self.no_launcher_var.get())

        self.stop_event.clear()
        self.log_queue = queue.Queue()
        self.build_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_status("Building...")
        self.result_holder = {"ok": False}
        self.worker = threading.Thread(
            target=_run_build_thread,
            args=(source_dir, out_dir, skip_deps, rebuild, no_launcher, jobs,
                  self.log_queue, self.stop_event, self.result_holder),
            daemon=True)
        self.worker.start()
        self.root.after(60, self._poll_queue)

    def _stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._set_status("Stopping...")

    def _poll_queue(self):
        tag_map = {"log": "info", "ok": "ok", "warn": "warn", "error": "error",
                   "step": "step", "info": "info"}
        try:
            while True:
                kind, msg, colour = self.log_queue.get_nowait()
                self.log_text.insert("end", msg + "\n", tag_map.get(kind, "info"))
                if kind in ("done", "failed"):
                    self._finish_build(kind == "done")
                    self.log_text.see("end")
                    return
        except queue.Empty:
            pass
        self.log_text.see("end")
        if self.worker and self.worker.is_alive():
            self.root.after(60, self._poll_queue)
        else:
            self._finish_build(self.result_holder.get("ok", False))

    def _finish_build(self, success):
        self.build_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_status("Build succeeded." if success else "Build failed / stopped.")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

# ---------------------------------------------------------------------------
# Headless log tee (writes the queue stream to a file + stdout)
# ---------------------------------------------------------------------------

def _install_log_tee(log_queue, log_path):
    """Return a stop() callable that drains log_queue to stdout and log_path."""
    fh = open(log_path, "w", encoding="utf-8")

    def stop():
        try:
            while True:
                kind, msg, colour = log_queue.get_nowait()
                line = ANSI_RE.sub("", msg)
                print(f"{colour}{msg}{RESET}", flush=True)
                fh.write(line + "\n")
                if kind in ("done", "failed"):
                    break
        except queue.Empty:
            pass
        fh.close()

    return stop

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ringout_build.py",
        description="RingOut Windows builder (GUI by default; --headless for no GUI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python ringout_build.py                       # launch the GUI
              python ringout_build.py --headless build        # build, clone from GitHub
              python ringout_build.py --headless build --source C:\\src\\RingOut
              python ringout_build.py --headless build --out C:\\RingOut --jobs 8
            """))
    p.add_argument("--headless", metavar="MODE", nargs="?", const="build",
                   choices=["build"],
                   help="run without a GUI (use 'build'); default action is 'build'")
    p.add_argument("--source", type=Path, default=None,
                   help="existing RingOut source root (must contain ModernGekko/CMakeLists.txt); "
                        "if omitted, the repo is cloned from GitHub")
    p.add_argument("--out", type=Path, default=None,
                   help="output package folder (default: ./RingOut-windows)")
    p.add_argument("--skip-deps", action="store_true",
                   help="do not download/install missing tools; fail if absent")
    p.add_argument("--rebuild", action="store_true",
                   help="clean the CMake build directories before configuring")
    p.add_argument("--no-launcher", action="store_true",
                   help="skip building the RingOut.exe launcher")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)),
                   help="parallel build jobs (default: CPU count)")
    p.add_argument("--log", type=Path, default=None,
                   help="headless mode only: also write the build log to this file")
    return p.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)

    if args.headless:
        log_queue: "queue.Queue" = queue.Queue()
        stop_event = threading.Event()
        tee = None
        if args.log:
            tee = _install_log_tee(log_queue, args.log)
        result_holder = {"ok": False}
        t = threading.Thread(
            target=_run_build_thread,
            args=(args.source, args.out, args.skip_deps, args.rebuild,
                  args.no_launcher, args.jobs, log_queue, stop_event, result_holder),
            daemon=False)
        t.start()
        t.join()
        if tee:
            tee()
        else:
            try:
                while True:
                    kind, msg, colour = log_queue.get_nowait()
                    print(f"{colour}{msg}{RESET}", flush=True)
                    if kind in ("done", "failed"):
                        break
            except queue.Empty:
                pass
        return 0 if result_holder.get("ok", False) else 1

    # GUI mode: import tkinter lazily so --headless works without it.
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        print(f"{RED}Tkinter is not available ({exc}).{RESET}", file=sys.stderr)
        print(f"{YELLOW}Run with --headless build for a no-GUI build.{RESET}", file=sys.stderr)
        return 1
    app = BuildApp(tk, ttk)
    app.run()
    return 0

if __name__ == "__main__":
    sys.exit(main())



