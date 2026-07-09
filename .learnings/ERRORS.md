# Errors

Command failures and integration errors.

---

## [ERR-20250623-001] test_path_safety Windows cross-platform assertions

**Logged**: 2026-06-23T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
`test_path_safety.py` 4 cases failed on Windows: `os.path.abspath` prepends `C:` drive letter, `os.path.normpath` does not.

### Error
```
C:\home\user\project\src\main.py != \home\user\project\src\main.py
```

### Context
- `_safe_path` internally uses `os.path.abspath` (adds drive letter on Windows)
- Tests compared output against `os.path.normpath` (no drive letter)
- 4 out of 10 path tests affected

### Resolution
- **Resolved**: 2026-06-23T18:05:00+08:00
- **Commit**: 26130e9
- **Notes**: Changed assertions to `os.path.abspath(os.path.join(...))` for cross-platform consistency.

---
