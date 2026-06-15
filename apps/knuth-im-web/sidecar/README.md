# Knuth IM Sidecar

Packaged desktop builds expect a PyInstaller `onedir` backend artifact here,
with the executable at `sidecar/knuth-im/knuth-im` on macOS/Linux or
`sidecar/knuth-im/knuth-im.exe` on Windows. Development builds launch the
workspace package with `uv run knuth-im`; release packaging should replace this
placeholder with the platform-specific backend artifact.
