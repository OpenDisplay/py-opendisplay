# Rules for whoever executes a plan in this folder

These plans are written by one session and often executed by another, possibly a smaller
model. The plan carries the design. Your job is to build exactly what it says and to report
honestly where reality differs.

## Before you start

1. Read `../specs/2026-11-15-v3-architecture-design.md`, including its last section
   "Status and findings". It is the newest record of what is true.
2. Read the plan you are executing, top to bottom, once.
3. Fetch the sources of truth and note their commit in your first ledger line:
   - firmware: `cd ~/Developer/OpenDisplay/Firmwares/Firmware_Unified && git fetch upstream`,
     read `upstream/main` (the plans were written against `f6d3a83`, 2026-11-15);
   - website reference code: `cd ~/Developer/OpenDisplay/Web && git fetch origin`,
     read `origin/v3/website` (written against `b7df514`).
   If either moved, read the diff of the files your plan cites before you rely on them, and
   write down what changed.

## While you work

- **One branch per task or per pull request, off current `main`.** No worktrees. Stage files
  by name. Commit with `uv run git commit` (the hook needs `prek` from the venv on PATH).
- **Test first.** Write the test, run it, see it fail for the reason you expect, then write
  the code. A test that passes before the code exists tests nothing: fix the test.
- **Tests must be able to fail.** After a task, switch the feature off (monkeypatch it to a
  no-op) and run its tests. Any that still pass are either "unchanged behaviour" tests, which
  is fine, or they test nothing, in which case delete or fix them.
- **No shims for private names.** When code moves, tests move with it.
- **Lint limits are not raised.** `facade.py` and `cli.py` are close to pylint's 1,500 lines.
  If your change does not fit, first move something out in a separate refactor commit.
- **Public API only grows.** Regenerate the snapshot only for names you add
  (`UPDATE_API_SNAPSHOT=1 uv run pytest tests/unit/test_public_api_snapshot.py`) and check
  that the diff of `tests/unit/data/public_api.json` shows additions only.
- **Every task ends with** `uv run pytest -q` and `uv run prek run --all-files`, both clean.

## When the plan and reality disagree

This is where the earlier pieces needed judgment, and where a wrong guess is expensive.

- **Hardware answers differently than the plan says:** record the exact frame in the ledger
  and in `scratch/`, do not map it to something that looks close, and stop that task.
- **The firmware header says something else than the plan:** the header wins. Record it.
  (This happened: a plan called a field unsigned, the header says signed.)
- **A step cannot work as written** (a helper does not exist, a signature differs): make the
  smallest change that keeps the plan's intent, and write a `Ruling:` line in the ledger
  saying what you decided, why, and what it costs if wrong.
- **Anything in a plan's "Decisions to confirm" list is not yours to settle.** If the user has
  not confirmed it, ask before building on it.

## Things that can hurt a device or the user

Ask before doing any of these, every time:

- flashing firmware, or anything that erases a device's config;
- writing a config that contains an encryption key to a firmware 3.0 device (3.0 adopts a 2.x
  key as its device key; until SEC2 support exists the library cannot open a session, and the
  device needs a factory reset);
- a Bluetooth firmware update on an EFR32BG22 tag (no USB port; recovery needs a debugger);
- pushing, opening or merging a pull request.

Never print a device key. `device_inventory.csv` holds them; read a key into a shell variable
and pass it on, and mask 32-hex-digit strings in anything you echo.

## Waiting for CI

Use `gh run watch <run id> --exit-status`. Do not write
`gh pr checks --watch | tail && gh pr merge`: before the checks are registered it prints
"no checks reported" and the pipe turns that into success.

## The ledger

Keep `.superpowers/sdd/<plan name>/progress.md` as you go: one line per finished task with the
commit range and the test result, every `Ruling:`, every hardware finding. It is git-ignored,
so at the end copy anything a later piece needs into the architecture spec's
"Status and findings" section.

## At the end of a piece

One independent review of the whole piece by the most capable model available, pointed at the
ledger's rulings and at the plan's "Review Focus". The two reviews so far found a wrong
firmware fact, phantom events, a silent data loss and several tests that could not fail.
