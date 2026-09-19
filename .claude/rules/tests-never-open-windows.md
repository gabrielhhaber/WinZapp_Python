---
paths:
  - "tests/**/*.py"
  - "pytest.ini"
  - ".github/workflows/*.yml"
---

# Tests never open windows

**Read `docs/traps/tests-never-open-windows.md` before changing these files.** Short form:

Frames go through `hidden_frame()`; real dialogs carry the `wxgui` marker. `--run-wx-gui` is for CI only — never on a developer machine, never handed to an agent or script. CI must pass it or the dialog tests are silently dropped.
