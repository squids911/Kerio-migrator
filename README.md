# Kerio Migrator

IMAP to Kerio Connect mailbox migration tool with CSV account selection, Kerio Admin API account creation, duplicate detection, speed limiting, and a four-step Tkinter wizard.

## Windows EXE

Every push to `main` that changes `imap_migrator.py` or `build.bat` starts the GitHub Actions workflow:

- `.github/workflows/build-windows-exe.yml`
- runner: `windows-latest`
- Python: `3.12`
- build: PyInstaller, one-file, windowed application
- artifact: `IMAP_Migrator-windows`

The workflow can also be started manually from the **Actions** tab using **Run workflow**. The generated `IMAP_Migrator.exe` is uploaded as a workflow artifact for 30 days.

## Local build

On Windows, run:

```bat
build.bat
```

The local build creates `dist\\IMAP_Migrator.exe`.
