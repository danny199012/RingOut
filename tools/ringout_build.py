#!/usr/bin/env python3
"""RingOut build helper -- a small GUI for compiling the RingOut runtime.

A single-file, standard-library-only tool that drives the project's CMake
build in three modes and can detect source changes and rebuild. It exists so a
player who downloaded the source can produce a working runtime without
hand-writing the CMake/Ninja invocation, and without installing anything
beyond a Python 3 that ships with Tkinter (CPython on Windows and most Linux
distros does).

What it builds
---------------
The RingOut runtime is the ModernGekko CMake project. Its primary targets are:

  moderngekko-launcher  the imgui launcher (RingOut.exe / RingOut)
  moderngekko-run        the Dolphin-based runtime (moderngekko-run.exe)
  moderngekko-port       the first-run module build driver
  dolrecomp              the GameCube PowerPC static recompiler

Build modes
-----------
1. Native Linux  -- cmake + ninja, like README "Building from source".
2. Native Windows -- cmake + ninja with the bundled or installed MSVC/Clang.
3. Windows cross-compile from Linux -- the MinGW toolchain file, like the
   release workflow in .github/workflows/windows-cross.yml.

Change detection
---------------
A "Check for changes" button runs `git status --porcelain` (when the source
tree is a git checkout) and also records the newest mtime of every tracked
source file, so a rebuild can be offered when anything changed since the last
build. The "Build" button always rebuilds; "Build if changed" only rebuilds
when something changed.

Usage
-----
  python3 ringout_build.py                 # launch the GUI
  python3 ringout_build.py --source PATH   # pre-set the source root
  python3 ringout_build.py --headless build # run one build with no GUI

The GUI never blocks: builds run on a worker thread and their stdout/stderr
stream into the log pane in real time.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# tkinter is imported lazily in main() so the headless --headless build/check
# paths work on a Python without Tkinter (e.g. a minimal server install, or
# this container). Only the GUI actually needs it.
__version__ = "1.0.0"

# The CMake targets that make up a usable runtime. The launcher is the
# player-facing entry point; moderngekko-run is the Dolphin runtime it spawns;
# moderngekko-port drives the first-run disc recompilation; dolrecomp is the
# recompiler binary that port tool invokes.
DEFAULT_TARGETS = ["moderngekko-launcher", "moderngekko-run",
                   "moderngekko-port", "dolrecomp"]

# Build mode identifiers shown in the GUI dropdown.
MODE_NATIVE_LINUX = "Native Linux (cmake + ninja)"
MODE_NATIVE_WINDOWS = "Native Windows (cmake + ninja, needs MSVC/Clang)"
MODE_NATIVE_WINDOWS_VS = "Native Windows (Visual Studio generator)"
MODE_WINDOWS_CROSS = "Windows cross-compile from Linux (MinGW)"


def default_source_root() -> Path:
    """Find the repository root by looking for ModernGekko/CMakeLists.txt.

    Searches upward from this script's directory, then the current working
    directory, so the tool works whether it was launched from inside the
    checkout or copied elsewhere.
    """
    candidates: list[Path] = []
    here = Path(__file__).resolve().parent
    candidates.append(here)
    candidates.append(here.parent)
    candidates.append(Path.cwd())
    candidates.append(Path.cwd().parent)
    for base in candidates:
        if (base / "ModernGekko" / "CMakeLists.txt").is_file():
            return base
    return Path.cwd()


def find_tool(name: str, extra_dirs: Optional[list[Path]] = None) -> Optional[str]:
    """Locate an executable on PATH or in extra_dirs, or None if absent."""
    found = shutil.which(name)
    if found:
        return found
    if extra_dirs:
        for d in extra_dirs:
            candidate = d / name
            if candidate.is_file():
                return str(candidate)
            # On Windows, try the .exe suffix too.
            candidate_exe = d / (name + ".exe")
            if candidate_exe.is_file():
                return str(candidate_exe)
    return None


class BuildConfig:
    """All the parameters for one build, resolved from the mode + source root."""

    def __init__(self, source_root: Path, mode: str, build_dir: Optional[Path],
                 targets: list[str], extra_cmake_args: list[str],
                 jobs: Optional[int]):
        self.source_root = Path(source_root)
        self.mode = mode
        self.targets = list(targets)
        self.extra_cmake_args = list(extra_cmake_args)
        # Default build directory sits beside the source so a clean checkout
        # stays clean, and so CMake's relative paths in error logs are short.
        if build_dir is None:
            if mode == MODE_WINDOWS_CROSS:
                self.build_dir = self.source_root / "build-windows-cross"
            else:
                self.build_dir = self.source_root / "build"
        else:
            self.build_dir = Path(build_dir)
        self.jobs = jobs

    def required_tools(self) -> list[str]:
        if self.mode == MODE_NATIVE_LINUX:
            return ["cmake", "ninja"]
        if self.mode == MODE_NATIVE_WINDOWS:
            return ["cmake", "ninja"]
        if self.mode == MODE_NATIVE_WINDOWS_VS:
            # The Visual Studio generator does not need ninja; it uses MSBuild.
            return ["cmake"]
        if self.mode == MODE_WINDOWS_CROSS:
            return ["cmake", "ninja", "x86_64-w64-mingw32-gcc"]
        return []

    def _missing_compiler(self) -> Optional[str]:
        """Return a description of the missing native compiler, or None.

        Relevant for both native Windows modes: the project's Windows release
        is cross-compiled from Linux with MinGW, so a native Windows build
        needs either Visual Studio's cl.exe or a clang.exe on PATH.
        """
        if self.mode not in (MODE_NATIVE_WINDOWS, MODE_NATIVE_WINDOWS_VS):
            return None
        for compiler in ("cl", "clang", "clang++", "gcc", "g++"):
            if find_tool(compiler, [self.source_root]):
                return None
        return ("No C/C++ compiler found. Install one of:\n"
                "  - Visual Studio 2022 with the 'Desktop development with C++' "
                "workload, then run this tool from an 'x64 Native Tools Command "
                "Prompt for VS' (so cl.exe is on PATH),\n"
                "  - LLVM/Clang (https://github.com/llvm/llvm-project/releases), "
                "or\n"
                "  - MinGW-w64 (https://www.mingw-w64.org/).\n"
                "Alternatively, use WSL and the 'Windows cross-compile from "
                "Linux' mode, which is how the official release is built.")

    def missing_tools(self) -> list[str]:
        # Search the source root too, so tools downloaded by the resolver
        # (ninja.exe, cmake.exe dropped next to the source) are found.
        extra = [self.source_root]
        return [t for t in self.required_tools()
                if find_tool(t, extra) is None]

    def _tool_path(self, name: str) -> str:
        """Resolve a tool to its full path, preferring the source root copy."""
        found = find_tool(name, [self.source_root])
        return found if found else name

    def configure_command(self) -> list[str]:
        """The cmake -S ... -B ... invocation for this mode."""
        cmd = [self._tool_path("cmake"), "-S", str(self.source_root / "ModernGekko"),
               "-B", str(self.build_dir)]
        if self.mode == MODE_NATIVE_WINDOWS_VS:
            # Use the Visual Studio 17 2022 generator (VS 2022) with x64.
            # This does not need ninja; MSBuild is used instead. The user must
            # run from a Developer Command Prompt so cl.exe is on PATH.
            cmd += ["-G", "Visual Studio 17 2022", "-A", "x64"]
        else:
            cmd += ["-G", "Ninja"]
            # If ninja was downloaded into the source root (and is not on PATH),
            # tell CMake exactly where it is, or it fails with "unable to find a
            # build program corresponding to Ninja".
            ninja_path = find_tool("ninja", [self.source_root])
            if ninja_path and shutil.which("ninja") is None:
                cmd += [f"-DCMAKE_MAKE_PROGRAM={ninja_path}"]
        cmd += ["-DCMAKE_BUILD_TYPE=Release"]
        if self.mode in (MODE_NATIVE_WINDOWS, MODE_NATIVE_WINDOWS_VS):
            # MSVC flags that make this project build cleanly on Windows.
            # /permissive breaks some of Dolphin's headers; /wd4067 suppresses
            # "unexpected tokens after preprocessor directive" (from the
            # /Zc:preprocessor mode the project requires), and /wd4804 /wd4805
            # suppress unsafe bool->int conversions in vendored code.
            cmd += ['-DCMAKE_CXX_FLAGS=/Zc:preprocessor /wd4067 /wd4804 /wd4805']
        if self.mode == MODE_WINDOWS_CROSS:
            toolchain = self.source_root / "cmake" / "toolchains" / \
                "mingw-x86_64.cmake"
            cmd += [f"-DCMAKE_TOOLCHAIN_FILE={toolchain}"]
            # The release workflow's exact flags. USE_SYSTEM_LIBS=OFF keeps
            # the vendored dependencies; the ENABLE_* flags turn off the
            # Dolphin features this recomp core does not ship.
            cmd += ["-DUSE_SYSTEM_LIBS=OFF", "-DBUILD_TESTING=OFF",
                    "-DENABLE_QT=OFF", "-DENABLE_TESTS=OFF",
                    "-DENABLE_ANALYTICS=OFF", "-DENABLE_AUTOUPDATE=OFF"]
        else:
            cmd += ["-DBUILD_TESTING=OFF", "-DENABLE_QT=OFF",
                    "-DENABLE_TESTS=OFF", "-DENABLE_ANALYTICS=OFF",
                    "-DENABLE_AUTOUPDATE=OFF"]
        cmd += self.extra_cmake_args
        return cmd

    def build_command(self) -> list[str]:
        """The cmake --build invocation for this mode."""
        cmd = [self._tool_path("cmake"), "--build", str(self.build_dir)]
        # When DolRecomp is built separately with clang, exclude it from the
        # main MSVC build so MSVC never tries to compile its POSIX-header C.
        targets = list(self.targets)
        if self.needs_separate_dolrecomp():
            targets = [t for t in targets if t != "dolrecomp"]
        if targets:
            cmd += ["--target", *targets]
        if self.jobs is not None:
            cmd += ["--parallel", str(self.jobs)]
        else:
            cmd += ["--parallel"]
        return cmd

    # -- DolRecomp (built separately with clang on native Windows) -----------

    def dolrecomp_source(self) -> Path:
        """The DolRecomp source directory (sibling or vendored)."""
        sibling = self.source_root / "DolRecomp"
        if sibling.is_dir():
            return sibling
        return self.source_root / "ModernGekko" / "vendor" / "dolphin" / "DolRecomp"

    def dolrecomp_build_dir(self) -> Path:
        return self.build_dir / "dolrecomp-clang"

    def needs_separate_dolrecomp(self) -> bool:
        """True when DolRecomp must be built separately with clang+Ninja.

        DolRecomp's C uses POSIX headers that MSVC lacks, so under a native
        Windows MSVC build it must be configured and built on its own with
        clang + Ninja (llvm-mingw provides both). The cross-compile and Linux
        modes build it inline with the rest of the project.
        """
        return self.mode in (MODE_NATIVE_WINDOWS, MODE_NATIVE_WINDOWS_VS)

    def dolrecomp_configure_command(self) -> list[str]:
        """Configure DolRecomp with clang + Ninja in its own build dir."""
        cmd = [self._tool_path("cmake"), "-S", str(self.dolrecomp_source()),
               "-B", str(self.dolrecomp_build_dir()), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++"]
        ninja_path = find_tool("ninja", [self.source_root])
        if ninja_path and shutil.which("ninja") is None:
            cmd += [f"-DCMAKE_MAKE_PROGRAM={ninja_path}"]
        return cmd

    def dolrecomp_build_command(self) -> list[str]:
        cmd = [self._tool_path("cmake"), "--build",
               str(self.dolrecomp_build_dir()), "--target", "dolrecomp"]
        if self.jobs is not None:
            cmd += ["--parallel", str(self.jobs)]
        else:
            cmd += ["--parallel"]
        return cmd

    def dolrecomp_binary(self) -> Path:
        return self.dolrecomp_build_dir() / ("dolrecomp" + self.binary_suffix())

    def binary_suffix(self) -> str:
        return ".exe" if self.mode in (MODE_NATIVE_WINDOWS,
                                      MODE_NATIVE_WINDOWS_VS,
                                      MODE_WINDOWS_CROSS) else ""

    def expected_binaries(self) -> list[Path]:
        suffix = self.binary_suffix()
        names = [t + suffix for t in self.targets]
        binaries = [self.build_dir / name for name in names]
        # When DolRecomp is built separately, its binary lands in the
        # dolrecomp-clang subdirectory, not the main build dir.
        if self.needs_separate_dolrecomp():
            for i, name in enumerate(names):
                if name.startswith("dolrecomp"):
                    binaries[i] = self.dolrecomp_binary()
        return binaries


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def git_is_checkout(root: Path) -> bool:
    return (root / ".git").exists() or (root / ".git").is_dir()


def git_changed_files(root: Path) -> list[str]:
    """Return paths that git reports as modified/untracked, or [] on error.

    Only used to summarize for the user; the rebuild decision also uses
    mtimes so it works on a non-git source tarball.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-z"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return []
        return [line for line in out.stdout.split("\0") if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def newest_source_mtime(root: Path) -> float:
    """Newest mtime of the source files CMake would compile.

    Walks ModernGekko and DolRecomp for .cpp/.c/.h/.hpp/.cmake/CMakeLists.txt
    and returns the maximum mtime. Used together with the newest build-output
    mtime to decide whether a rebuild is needed. Falls back to 0.0 if nothing
    matched (e.g. an empty checkout), which means "always rebuild".
    """
    exts = {".cpp", ".c", ".h", ".hpp", ".cmake"}
    newest = 0.0
    for sub in ("ModernGekko", "DolRecomp"):
        base = root / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                if path.is_file() and (path.suffix in exts or
                                       path.name == "CMakeLists.txt"):
                    newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def newest_build_mtime(build_dir: Path) -> float:
    """Newest mtime of any file in the build directory, or 0.0 if absent."""
    if not build_dir.is_dir():
        return 0.0
    newest = 0.0
    for path in build_dir.rglob("*"):
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def needs_rebuild(config: BuildConfig) -> tuple[bool, str]:
    """Decide whether a rebuild is warranted and explain why.

    A rebuild is needed when the source is newer than the build output, or
    when any expected binary is missing. The explanation is shown in the GUI.
    """
    missing = [str(p) for p in config.expected_binaries() if not p.exists()]
    if missing:
        return True, f"missing build output: {', '.join(missing)}"
    src_newest = newest_source_mtime(config.source_root)
    build_newest = newest_build_mtime(config.build_dir)
    if src_newest > build_newest:
        return True, (f"source files are newer than the last build "
                      f"(source {time.ctime(src_newest)} > "
                      f"build {time.ctime(build_newest)})")
    return False, "build is up to date"


# ---------------------------------------------------------------------------
# Dependency resolver
# ---------------------------------------------------------------------------

# Download URLs for the tools that are hardest to get on Windows. These are the
# official release assets; the tool downloads, extracts, and drops the
# executable next to the source root so the build finds it without a PATH edit.
TOOL_DOWNLOADS = {
    "ninja": {
        "url": "https://github.com/ninja-build/ninja/releases/download/"
               "v1.12.1/ninja-win.zip",
        "exe": "ninja.exe",
    },
    "cmake": {
        # The portable Windows zip is used (not the installer) so nothing
        # touches the system PATH or registry.
        "url": "https://github.com/Kitware/CMake/releases/download/"
               "v3.30.3/cmake-3.30.3-windows-x86_64.zip",
        "exe": "bin/cmake.exe",
    },
}


def _download(url: str, dest: Path, log) -> bool:
    """Download a URL to dest, streaming progress to log. Returns success."""
    import urllib.request
    log(f"  downloading {url}\n")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    log(f"\r  {done // 1024} / {total // 1024} KiB")
                else:
                    log(f"\r  {done // 1024} KiB")
            log("\n")
        return True
    except Exception as exc:
        log(f"  [download failed: {exc}]\n")
        return False


class DependencyResolver:
    """Checks for and downloads missing build tools before a build starts.

    Only handles tools that have a portable Windows download (ninja, cmake).
    The MinGW cross-compiler cannot be downloaded this way (it is a large
    multi-package toolchain), so the cross mode still requires a manual
    install and reports a clear error if it is missing.
    """

    def __init__(self, config: BuildConfig, log_queue, stop_event):
        self.config = config
        self.log_queue = log_queue
        self.stop_event = stop_event

    def _log(self, text: str) -> None:
        self.log_queue.put(text)

    def resolve(self) -> bool:
        """Ensure all required tools are available. Returns True if ready."""
        missing = self.config.missing_tools()
        compiler_msg = self.config._missing_compiler()
        if not missing and compiler_msg is None:
            return True
        self._log("\n=== dependency check ===\n")
        if missing:
            self._log(f"missing: {', '.join(missing)}\n")
        if compiler_msg:
            self._log(compiler_msg + "\n")
        # Only the native Windows mode can auto-download build tools; Linux/macOS
        # users use their package manager, and cross-compile needs a full MinGW
        # toolchain that is too large to fetch here.
        if self.config.mode != MODE_NATIVE_WINDOWS:
            self._log("Install the missing tools with your package "
                       "manager:\n")
            if self.config.mode == MODE_NATIVE_LINUX:
                self._log("  Debian/Ubuntu: sudo apt install cmake "
                           "ninja-build\n  Arch: sudo pacman -S cmake ninja\n")
            elif self.config.mode == MODE_WINDOWS_CROSS:
                self._log("  Debian/Ubuntu: sudo apt install "
                           "gcc-mingw-w64-x86-64-posix "
                           "g++-mingw-w64-x86-64-posix cmake ninja-build\n"
                           "  Arch: sudo pacman -S mingw-w64-gcc cmake ninja\n")
            return False
        # Native Windows: a missing compiler cannot be auto-downloaded, so
        # report that and stop before attempting any downloads.
        if compiler_msg:
            return False
        # Auto-download cmake/ninja into the source root.
        for tool in missing:
            if tool not in TOOL_DOWNLOADS:
                self._log(f"  [no auto-download available for {tool}; install "
                           f"it manually]\n")
                return False
            if not self._fetch_tool(tool):
                return False
        # Re-check after fetching.
        still_missing = self.config.missing_tools()
        if still_missing:
            self._log(f"  [still missing after download: "
                      f"{', '.join(still_missing)}]\n")
            return False
        self._log("  all required tools are now available\n")
        return True

    def _fetch_tool(self, tool: str) -> bool:
        info = TOOL_DOWNLOADS[tool]
        archive = self.config.source_root / f"{tool}.zip"
        self._log(f"  fetching {tool}...\n")
        if not _download(info["url"], archive, self._log):
            return False
        if self.stop_event.is_set():
            return False
        # Extract just the executable we need into the source root.
        import zipfile
        try:
            with zipfile.ZipFile(archive) as zf:
                self._log(f"  extracting {info['exe']}...\n")
                zf.extract(info["exe"], self.config.source_root)
        except Exception as exc:
            self._log(f"  [extract failed: {exc}]\n")
            return False
        finally:
            try:
                archive.unlink()
            except OSError:
                pass
        exe_path = self.config.source_root / Path(info["exe"]).name
        self._log(f"  installed {tool} -> {exe_path}\n")
        return True


# ---------------------------------------------------------------------------
# Build runner
# ---------------------------------------------------------------------------

class BuildRunner:
    """Runs configure + build on a worker thread, streaming output to a queue.

    The GUI polls the queue from its main loop so the window stays responsive
    and the log updates in real time. A stop event lets the user cancel a
    running build (the subprocess is terminated).
    """

    def __init__(self, config: BuildConfig, log_queue: "queue.Queue[str]",
                 stop_event: threading.Event,
                 progress_cb: Optional[callable] = None):
        self.config = config
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.progress_cb = progress_cb
        self.returncode: Optional[int] = None

    def _log(self, text: str) -> None:
        self.log_queue.put(text)

    def _run(self, cmd: list[str], label: str) -> bool:
        self._log(f"\n=== {label} ===\n$ {' '.join(cmd)}\n")
        if self.stop_event.is_set():
            self._log("[cancelled before start]\n")
            return False
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(self.config.source_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8",
                errors="replace")
        except FileNotFoundError as exc:
            self._log(f"[failed to start] {exc}\n")
            return False
        assert proc.stdout is not None
        for line in proc.stdout:
            if self.stop_event.is_set():
                proc.terminate()
                self._log("[cancelled]\n")
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return False
            self._log(line)
        proc.wait()
        self.returncode = proc.returncode
        if proc.returncode != 0:
            self._log(f"[{label} failed with exit code {proc.returncode}]\n")
            return False
        self._log(f"[{label} succeeded]\n")
        return True

    def run(self) -> bool:
        resolver = DependencyResolver(self.config, self.log_queue,
                                      self.stop_event)
        if not resolver.resolve():
            return False
        # DolRecomp needs clang on native Windows builds (POSIX headers).
        if self.config.needs_separate_dolrecomp():
            if find_tool("clang", [self.config.source_root]) is None:
                self._log("[DolRecomp needs clang for POSIX headers, but "
                          "clang was not found. Install LLVM/Clang "
                          "(https://github.com/llvm/llvm-project/releases) "
                          "and put clang.exe on PATH, or use the Windows "
                          "cross-compile mode under WSL.]\n")
                return False
            if find_tool("ninja", [self.config.source_root]) is None:
                self._log("[DolRecomp needs ninja when built separately with "
                          "clang. Install ninja or let the tool download it "
                          "(click Build in Native Windows (cmake + ninja) "
                          "mode first to auto-download it).]\n")
                return False
        self._log(f"Source root: {self.config.source_root}\n")
        self._log(f"Build dir:  {self.config.build_dir}\n")
        self._log(f"Mode:       {self.config.mode}\n")
        self._log(f"Targets:    {', '.join(self.config.targets)}\n")
        self.config.build_dir.mkdir(parents=True, exist_ok=True)
        # On native Windows, build DolRecomp separately with clang+Ninja
        # before the main build, so the main build's MSVC step does not try
        # to compile DolRecomp's POSIX-header C.
        if self.config.needs_separate_dolrecomp():
            self._log("\n--- DolRecomp (clang + Ninja) ---\n")
            self.config.dolrecomp_build_dir().mkdir(parents=True,
                                                    exist_ok=True)
            if not self._run(self.config.dolrecomp_configure_command(),
                              "configure dolrecomp"):
                return False
            if not self._run(self.config.dolrecomp_build_command(),
                              "build dolrecomp"):
                return False
        if not self._run(self.config.configure_command(), "configure"):
            return False
        if not self._run(self.config.build_command(), "build"):
            return False
        # Report which expected binaries actually appeared.
        self._log("\n=== build output ===\n")
        for binary in self.config.expected_binaries():
            mark = "OK " if binary.exists() else "MISSING "
            self._log(f"  {mark}{binary}\n")
        return all(p.exists() for p in self.config.expected_binaries())


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

# tkinter is imported lazily inside the GUI functions below, so that importing
# this module (for --help or --headless) never fails on a Python without Tk.


def _open_folder(path: Path) -> None:
    """Open a folder in the platform file manager."""
    import platform
    sysname = platform.system()
    try:
        if sysname == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sysname == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        pass


class BuildApp:
    """The Tkinter GUI. Holds the config widgets and drives the BuildRunner."""

    def __init__(self, root: tk.Tk, initial_source: Optional[Path] = None):
        self.root = root
        root.title(f"Ring Out build helper v{__version__}")
        root.geometry("860x620")
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.runner: Optional[BuildRunner] = None
        self._build_controls(initial_source)
        self._build_log_pane()
        self._build_status_bar()
        # Poll the log queue ~every 60ms so output streams without freezing.
        self.root.after(60, self._drain_log_queue)

    def _build_controls(self, initial_source: Optional[Path]) -> None:
        box = ttk.LabelFrame(self.root, text="Build configuration")
        box.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(box, text="Source root:").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=4)
        self.source_var = tk.StringVar(
            value=str(initial_source or default_source_root()))
        ttk.Entry(box, textvariable=self.source_var, width=60).grid(
            row=0, column=1, sticky=tk.EW, padx=4, pady=4)
        ttk.Button(box, text="Browse...", command=self._browse_source).grid(
            row=0, column=2, padx=4, pady=4)
        ttk.Label(box, text="Build mode:").grid(
            row=1, column=0, sticky=tk.W, padx=4, pady=4)
        self.mode_var = tk.StringVar(value=self._default_mode())
        ttk.OptionMenu(box, self.mode_var, self.mode_var.get(),
                       MODE_NATIVE_LINUX, MODE_NATIVE_WINDOWS,
                       MODE_NATIVE_WINDOWS_VS, MODE_WINDOWS_CROSS).grid(
            row=1, column=1, sticky=tk.W, padx=4, pady=4)
        ttk.Label(box, text="CMake targets:").grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=4)
        self.targets_var = tk.StringVar(value=" ".join(DEFAULT_TARGETS))
        ttk.Entry(box, textvariable=self.targets_var, width=60).grid(
            row=2, column=1, sticky=tk.EW, padx=4, pady=4)
        ttk.Label(box, text="Extra CMake args:").grid(
            row=3, column=0, sticky=tk.W, padx=4, pady=4)
        self.extra_var = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.extra_var, width=60).grid(
            row=3, column=1, sticky=tk.EW, padx=4, pady=4)
        ttk.Label(box, text="Jobs (blank = all):").grid(
            row=4, column=0, sticky=tk.W, padx=4, pady=4)
        self.jobs_var = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.jobs_var, width=8).grid(
            row=4, column=1, sticky=tk.W, padx=4, pady=4)
        box.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.root)
        btns.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(btns, text="Build", command=self._on_build).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Build if changed",
                   command=self._on_build_if_changed).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Check for changes",
                   command=self._on_check_changes).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Open build folder",
                   command=self._on_open_build).pack(
            side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(btns, text="Stop",
                                   command=self._on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Clear log", command=self._on_clear).pack(
            side=tk.RIGHT, padx=2)

    def _build_log_pane(self) -> None:
        self.log = tk.Text(self.root, wrap=tk.NONE, state=tk.DISABLED,
                           bg="#1e1e1e", fg="#d4d4d4",
                           insertbackground="#d4d4d4", font=("Consolas", 10))
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        scroll = ttk.Scrollbar(self.log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)

    def _build_status_bar(self) -> None:
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, side=tk.BOTTOM)

    def _default_mode(self) -> str:
        if sys.platform == "win32":
            # The Visual Studio generator is the most common Windows setup and
            # does not require a separate ninja install.
            return MODE_NATIVE_WINDOWS_VS
        return MODE_NATIVE_LINUX

    def _make_config(self) -> BuildConfig:
        targets = [t for t in self.targets_var.get().split() if t]
        extra = [a for a in self.extra_var.get().split() if a]
        jobs_str = self.jobs_var.get().strip()
        jobs = int(jobs_str) if jobs_str.isdigit() else None
        return BuildConfig(
            source_root=Path(self.source_var.get()),
            mode=self.mode_var.get(),
            build_dir=None, targets=targets or DEFAULT_TARGETS,
            extra_cmake_args=extra, jobs=jobs)

    def _set_busy(self, busy: bool) -> None:
        self.stop_btn.config(state=tk.NORMAL if busy else tk.DISABLED)

    def _append_log(self, text: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(60, self._drain_log_queue)

    # -- actions ----------------------------------------------------------

    def _browse_source(self) -> None:
        d = filedialog.askdirectory(title="Select the Ring Out source root")
        if d:
            self.source_var.set(d)

    def _on_check_changes(self) -> None:
        config = self._make_config()
        if not (config.source_root / "ModernGekko" / "CMakeLists.txt").is_file():
            messagebox.showerror("Source root",
                                 "That folder does not contain "
                                 "ModernGekko/CMakeLists.txt")
            return
        need, why = needs_rebuild(config)
        git_changes = git_changed_files(config.source_root) \
            if git_is_checkout(config.source_root) else []
        msg = why
        if git_changes:
            n = len(git_changes)
            msg += f"\n\nGit reports {n} changed file" + \
                ("s" if n != 1 else "") + ":\n" + "\n".join(
                    git_changes[:20]) + ("\n..." if n > 20 else "")
        self.status_var.set("Up to date" if not need else "Rebuild needed")
        messagebox.showinfo("Change check", msg)

    def _on_open_build(self) -> None:
        config = self._make_config()
        config.build_dir.mkdir(parents=True, exist_ok=True)
        _open_folder(config.build_dir)

    def _on_clear(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    def _on_stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping...")

    def _start_build(self, only_if_changed: bool) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning("Busy", "A build is already running.")
            return
        config = self._make_config()
        if not (config.source_root / "ModernGekko" /
                "CMakeLists.txt").is_file():
            messagebox.showerror("Source root",
                                 "That folder does not contain "
                                 "ModernGekko/CMakeLists.txt")
            return
        if only_if_changed:
            need, why = needs_rebuild(config)
            if not need:
                self._append_log(f"[skipped] {why}\n")
                self.status_var.set("Up to date -- nothing to build")
                return
            self._append_log(f"[rebuilding] {why}\n")
        self.stop_event.clear()
        self.runner = BuildRunner(config, self.log_queue, self.stop_event)
        self._set_busy(True)
        self.status_var.set("Building...")
        self.worker = threading.Thread(target=self._worker_main, daemon=True)
        self.worker.start()

    def _worker_main(self) -> None:
        assert self.runner is not None
        ok = self.runner.run()
        self.root.after(0, lambda: self._on_build_done(ok))

    def _on_build_done(self, ok: bool) -> None:
        self._set_busy(False)
        if ok:
            self.status_var.set("Build succeeded.")
            self._append_log("\n*** BUILD SUCCEEDED ***\n")
        else:
            self.status_var.set("Build failed.")
            self._append_log("\n*** BUILD FAILED ***\n")

    def _on_build(self) -> None:
        self._start_build(only_if_changed=False)

    def _on_build_if_changed(self) -> None:
        self._start_build(only_if_changed=True)


# ---------------------------------------------------------------------------
# Headless mode + main
# ---------------------------------------------------------------------------

def run_headless(args: argparse.Namespace) -> int:
    """Run one build with no GUI, for scripting / CI use."""
    mode = {
        "linux": MODE_NATIVE_LINUX,
        "windows": MODE_NATIVE_WINDOWS,
        "vs": MODE_NATIVE_WINDOWS_VS,
        "cross": MODE_WINDOWS_CROSS,
    }[args.mode]
    config = BuildConfig(
        source_root=Path(args.source or default_source_root()),
        mode=mode, build_dir=Path(args.build_dir) if args.build_dir else None,
        targets=args.targets.split() if args.targets else DEFAULT_TARGETS,
        extra_cmake_args=args.cmake_args.split() if args.cmake_args else [],
        jobs=int(args.jobs) if args.jobs else None)

    def emit(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    class _Q:
        def put(self, text: str) -> None:
            emit(text)

    runner = BuildRunner(config, _Q(), threading.Event())
    return 0 if runner.run() else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ring Out build helper -- compile the runtime from source.")
    parser.add_argument("--source", help="path to the repository root")
    parser.add_argument("--headless", choices=["build", "check"],
                        help="run without a GUI")
    parser.add_argument("--mode", choices=["linux", "windows", "vs", "cross"],
                        default="linux", help="build mode (headless)")
    parser.add_argument("--build-dir", help="output build directory (headless)")
    parser.add_argument("--targets", help="space-separated CMake targets")
    parser.add_argument("--cmake-args", help="extra CMake args (headless)")
    parser.add_argument("--jobs", help="parallel jobs (headless)")
    args = parser.parse_args(argv)

    if args.headless == "build":
        return run_headless(args)
    if args.headless == "check":
        mode = {"linux": MODE_NATIVE_LINUX, "windows": MODE_NATIVE_WINDOWS,
                "vs": MODE_NATIVE_WINDOWS_VS,
                "cross": MODE_WINDOWS_CROSS}[args.mode]
        config = BuildConfig(
            source_root=Path(args.source or default_source_root()),
            mode=mode, build_dir=Path(args.build_dir) if args.build_dir else None,
            targets=args.targets.split() if args.targets else DEFAULT_TARGETS,
            extra_cmake_args=[], jobs=None)
        need, why = needs_rebuild(config)
        print(why)
        return 0 if not need else 2

    # GUI mode.
    try:
        import tkinter as tk  # noqa: F401  (used by BuildApp below)
        from tkinter import filedialog, messagebox, ttk  # noqa: F401
    except ImportError as exc:
        print(f"Could not import tkinter: {exc}\n"
              "Run with --headless build for a no-GUI build, or install a "
              "Tkinter-capable Python (e.g. python3-tk on Debian/Ubuntu).",
              file=sys.stderr)
        return 1
    # Inject into module globals so BuildApp's methods (which reference tk/ttk
    # as module-level names) can see them without each method re-importing.
    import sys as _sys
    _mod = _sys.modules[__name__]
    _mod.tk = tk
    _mod.filedialog = filedialog
    _mod.messagebox = messagebox
    _mod.ttk = ttk
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not start the GUI: {exc}\n"
              "Run with --headless build for a no-GUI build, or install a "
              "Tkinter-capable Python (e.g. python3-tk on Debian/Ubuntu).",
              file=sys.stderr)
        return 1
    BuildApp(root, Path(args.source) if args.source else None)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





