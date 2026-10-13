# nanowin — Nano for Windows

Windows flavor of Nano (bilingual offline accounting). Same language
(Python + Flet 0.28.3), same code, same database schema as the
[`nano`](https://github.com/bukamal/nano) Android tree — only the build
target and a thin platform layer differ.

- Shared business logic lives in `nano` and is pulled here with
  `tools/sync_from_nano.sh` (one direction only — see
  `docs/WINDOWS_BUILD_AR.md`, section 5).
- Windows-specific files owned by this repo: `NANOWIN.md`,
  `docs/WINDOWS_BUILD_AR.md`, `tools/sync_from_nano.sh`,
  `.github/workflows/build-windows.yml`,
  `src/nano_offline/core/platform_capabilities.py`.
- CI (`Build Windows EXE`) runs the quality gate and uploads the exe as
  the `nano-windows-exe` artifact.
