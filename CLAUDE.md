# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A small data catalog (Python + Streamlit + SQLite, single process) for registering local data files (CSV / Parquet / JSON / JSONL) and managing descriptions, tags, properties, lineage, data owner / steward roles, and "questions" (問い) that the data answers. UI text, docs, comments and commit messages are in Japanese. Design background: `docs/question-driven.md` (roles and the question-driven approach), `docs/plan.md` (implemented design), `README.md` (usage).

## Architecture

**Layers**: `pages/*.py` (Streamlit UI) → `catalog/ui.py` (shared widgets/helpers) → domain modules in `catalog/` (`repository`, `properties`, `roles`, `questions`, `lineage`, `auth`, `changelog`) → `catalog/db.py` / `catalog/tx.py`. Domain modules never import Streamlit; keep business rules there so they are testable.

**Write path conventions (follow these for any new write function):**
- Every write function takes `actor: User` and calls `auth.require(actor, Perm.X)` first. Permission levels: `VIEW < EDIT (editor) < MANAGE_ASSETS (power) < ADMIN`.
- Wrap the whole read-validate-write in `with write_tx(conn):` (`BEGIN IMMEDIATE`; nested calls join the outer tx). Validation such as diffs, lineage cycle checks, and "last admin remains" must happen inside the tx.
- Record changes with `changelog.log(conn, actor, entity, action, asset_id=..., target=..., old=..., new=...)` inside the same tx. Only log real changes.
- APIs are **diff-based** ("項目単位のあと勝ち"): update functions accept only changed fields (`None` = unchanged), tags and lineage take add/remove lists. Never "replace all with what the form shows".
- Connections are opened with `isolation_level=None`; `catalog/ui.get_conn()` returns a **new connection per call**. Never cache or share a connection across sessions/threads.

**UI conventions:**
- `app.py` is a router using `st.navigation`: it handles login / first-admin bootstrap / forced password change, and registers only the pages the user's role may see. New pages must be added there. `st.page_link` paths are relative to `app.py` (e.g. `"pages/2_アセット詳細.py"`) and fail if the page isn't registered for that role, so guard links with `auth.can(...)`.
- Every page starts with `conn, user = require_page(Perm.X)` (defense in depth). Pages don't call `st.set_page_config`.
- Edit forms use `ui.FormSnapshot(name, id, loader)`: widgets render from the snapshot, on save compare inputs to the snapshot and send only changed fields, then `snap.reset()`. `snap.stale_notice()` tells the user someone else changed the data.
- Free-text fields with suggestions use `st.selectbox(..., accept_new_options=True)` with the `ui.UNSET` sentinel. For "none" choices in selectboxes use `0`, not `None` (`None` is ambiguous with "nothing selected").

**Domain specifics that span files:**
- Re-registering an asset with the same name (or `repository.reingest_asset`) keeps the asset id, so all metadata survives; renamed columns are carried over via a `renames` map.
- Roles (`roles.py`): assignments are one row each, targeting a domain *or* an asset. Effective roles are computed at read time (`effective_assignments`): owners are inherited per responsibility scope (`OWNER_SCOPES`), stewards per role type. Role types are data in `role_types` (to allow a 3rd tier later).
- Asset status (`asset_status.py`, `assets.status`): 運用中 / 非推奨 / 廃止. It drives behaviour, so it is a fixed-value column rather than a property (admins can rename property choices). Changing it requires `MANAGE_ASSETS`. Retired assets are hidden from the home default, skipped by `incomplete_only`, and not suggested for questions; dependencies on non-active assets produce warnings (`repository.status_warnings`, `questions.hints`), never errors.
- Completeness (`properties.completeness`) = description + required properties + owner scope coverage + at least one steward. "No questions" is intentionally **not** part of completeness; it's shown as a separate soft indicator.
- Questions (`questions.py`): only the text is required. Missing details become `hints()` (nudges, never errors). Asset suggestions use character-bigram overlap (no morphological analysis).

**Schema changes** (`catalog/db.py`): `SCHEMA` uses `CREATE ... IF NOT EXISTS`. Bump `SCHEMA_VERSION`, add the old version to `UPGRADABLE_FROM`, and list added columns in `MIGRATIONS[old_version]` (applied idempotently before `SCHEMA` runs). DBs at versions not in `UPGRADABLE_FROM` are backed up to `catalog.db.bak-<timestamp>` and recreated empty. Add a test in `tests/test_db.py` for each upgrade path.

## Testing notes

- Tests use a temp file DB per test (`tests/conftest.py`) with role fixtures `admin` / `power` / `editor` / `viewer`.
- Concurrency behaviour is tested with multiple `connect(db_path)` connections and threads (`tests/test_concurrency.py`).
- UI flows have been verified with `streamlit.testing.v1.AppTest` (run via `app.py` with `at.session_state["user_id"] = ...` and `at.switch_page(...)`); these scripts are not in the repo. AppTest cannot enter new values into `accept_new_options` selectboxes, so seed suggestions via the API first.
