# Ring Out build helper

`ringout_build.py` is a small, dependency-free Python tool with a GUI for
compiling the Ring Out runtime from source, plus the ability to detect source
changes and rebuild. It uses only the Python standard library (Tkinter for the
GUI), so there is nothing to `pip install`.

## Quick start

```sh
python3 tools/ringout_build.py
```

Pick a build mode from the dropdown, confirm the source root (auto-detected),
and click **Build**. Build output streams into the log pane in real time.

## What it builds

The four CMake targets that make up a usable runtime:

| target | what it is |
| --- | --- |
| `moderngekko-launcher` | the imgui launcher (`RingOut.exe` / `RingOut`) |
| `moderngekko-run` | the Dolphin-based runtime |
| `moderngekko-port` | the first-run disc recompilation driver |
| `dolrecomp` | the GameCube PowerPC static recompiler |

## Build modes

| mode | when to use | needs |
| --- | --- | --- |
| Native Linux | building on Linux for Linux | `cmake`, `ninja`, `clang` or `gcc` |
| Native Windows | building on Windows for Windows | `cmake`, `ninja`, MSVC or Clang |
| Windows cross-compile | building on Linux for Windows | `cmake`, `ninja`, MinGW-w64 (`x86_64-w64-mingw32-gcc`) |

The Windows cross-compile mode uses the project's
`cmake/toolchains/mingw-x86_64.cmake` toolchain file and the exact flags from
`.github/workflows/windows-cross.yml`. On Ubuntu install the POSIX-thread
MinGW packages: `gcc-mingw-w64-x86-64-posix g++-mingw-w64-x86-64-posix`.

## Buttons

- **Build** — always reconfigure and rebuild.
- **Build if changed** — rebuild only when source files are newer than the last
  build output, or a target binary is missing.
- **Check for changes** — report whether a rebuild is needed and why, plus any
  `git status` changes (when the source is a git checkout).
- **Open build folder** — open the build directory in your file manager.
- **Stop** — cancel a running build.
- **Clear log** — clear the log pane.

## Headless mode (no GUI)

For scripting or a server without a display:

```sh
# build, native Linux
python3 tools/ringout_build.py --headless build --mode linux

# build, Windows cross-compile
python3 tools/ringout_build.py --headless build --mode cross

# just check whether a rebuild is needed (exit 0 = up to date, 2 = needed)
python3 tools/ringout_build.py --headless check --mode linux
```

Headless mode does not require Tkinter. The GUI falls back to a clear error
message if Tkinter is missing (install `python3-tk` on Debian/Ubuntu).

## Where the binaries end up

| mode | build directory | binaries |
| --- | --- | --- |
| Native Linux | `build/` | `build/moderngekko-launcher`, etc. |
| Native Windows | `build/` | `build/moderngekko-launcher.exe`, etc. |
| Windows cross-compile | `build-windows-cross/` | `build-windows-cross/*.exe` |

## Notes

- The tool reconfigures on every build. CMake is incremental, so a second
  build with no changes is fast.
- The first full build is large (the Dolphin runtime + vendored dependencies);
  expect several minutes on a multi-core machine.
- After building the runtime, you still need to recompile your game disc into a
  module on first run — that is what the launcher's first-run setup does, using
  the `dolrecomp` and `moderngekko-port` binaries this tool produces.
