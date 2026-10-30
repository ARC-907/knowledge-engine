# Bug log

Defects found in the engine, their root cause, the fix and the test that keeps
them fixed. One row per bug; newest first within each section. User-visible
fixes also appear in the [CHANGELOG](../CHANGELOG.md). Report new bugs as GitHub
issues; this log records the analysis once a fix lands.

Columns: **Found** — how it surfaced (audit, review, CI platform, test).
**Fix** — the pull request that fixed it.

## V2 (after 3.0.0)

| ID | Area | Symptom | Root cause | Found | Fix | Regression test |
| --- | --- | --- | --- | --- | --- | --- |
| B-025 | Knowledge | After `knowledge snapshot` detected a rename A→B, a new file at A was counted "existing" by `knowledge migrate` but never registered | The renamed record keeps the id derived from path A; migrate derived the same id for the new file and the insert hit `ON CONFLICT DO NOTHING` | Review of the evidence slice | [#36](https://github.com/ARC-907/knowledge-engine/issues/36) | `test_migrate_registers_a_new_file_at_a_renamed_path` |
| B-024 | Retrieval | On Windows every nested-entry lookup returned no owner (18 failing tests) | `os.path.normcase` also turns `/` into `\` on Windows, so prefix matching on `/` never fired | Windows CI | [#41](https://github.com/ARC-907/knowledge-engine/pull/41) | `test_ownership_keys_survive_windows_normcase` |
| B-023 | Retrieval | `fts` and `hybrid` returned a nested file twice; `kind` filters disagreed between modes ([#39](https://github.com/ARC-907/knowledge-engine/issues/39)) | FTS indexed a file under every enclosing entry, vector used the longest prefix after kind filtering, fusion keyed on `(entry_id, path)` | Review | [#41](https://github.com/ARC-907/knowledge-engine/pull/41) | `tests/test_nested_ownership.py` |
| B-022 | Tests | Both dashboard browser tests errored before reaching the browser | The fixture used `app.state.indexer`, which exists only inside the lifespan since 3.0 | Enabling the browser CI job | [#42](https://github.com/ARC-907/knowledge-engine/pull/42) | `dashboard (browser smoke)` CI job |
| B-021 | CI | Dashboard browser tests never ran anywhere, so B-022 went unnoticed | Tests were opt-in and no CI job set `KE_UI_TESTS=1` | Review ([#40](https://github.com/ARC-907/knowledge-engine/issues/40)) | [#42](https://github.com/ARC-907/knowledge-engine/pull/42) | Job fails when the tests are skipped or not collected |
| B-020 | Knowledge | Import cycle between `knowledge.contracts` and `knowledge.adapter` | Schema export in `contracts` imported the adapter protocol, which imports the contracts | CodeQL | [#43](https://github.com/ARC-907/knowledge-engine/pull/43) | Module `knowledge.schema` holds the export |

## 3.0 train

| ID | Area | Symptom | Root cause | Found | Fix | Regression test |
| --- | --- | --- | --- | --- | --- | --- |
| B-019 | CI | Build job failed after the migrations moved | Wheel-content check grepped the old `project_context/migrations/001_` path | CI | [#30](https://github.com/ARC-907/knowledge-engine/pull/30) | Wheel check covers every migration set |
| B-018 | Retrieval | CodeQL import cycle between embeddings factory and index | `open_index` lived in the factory and imported the index | CodeQL | [#29](https://github.com/ARC-907/knowledge-engine/pull/29) | `open_index` moved into the index module |
| B-017 | Registry | `registry.json` written with CRLF on Windows | Text-mode write translated newlines | Windows CI | [#27](https://github.com/ARC-907/knowledge-engine/pull/27) | `newline="\n"` write and test |
| B-016 | Core | `httpx.InvalidURL` escaped `OllamaClient` as an unhandled error | Only transport errors were mapped | Review | [#27](https://github.com/ARC-907/knowledge-engine/pull/27) | Malformed-URL tests for both embedding stacks |
| B-015 | Core | Corpus folders registered in a different order on Windows | `Path` sorting is case-insensitive there | Windows CI | [#27](https://github.com/ARC-907/knowledge-engine/pull/27) | Sort by `name` |
| B-014 | Tool host | Windows command lines lost backslashes | `shlex.split` in POSIX mode treats `\` as an escape | Windows CI | [#25](https://github.com/ARC-907/knowledge-engine/pull/25) | Platform-aware splitting test |
| B-013 | Project context | Pointer apply could write outside the project root | Target paths from DB rows were joined without a containment check | Audit | [#25](https://github.com/ARC-907/knowledge-engine/pull/25) | `ensure_within` tests |
| B-012 | Project context | Git option injection through MCP-supplied refs | `git diff` ran without `--` and refs were not validated | Audit | [#25](https://github.com/ARC-907/knowledge-engine/pull/25) | Ref validation tests |
| B-011 | Agent board | Admin routes (keys, providers, config) reachable without a key, behind wildcard CORS | Admin auth was tied to `require_key_for_post`, default off | Audit | [#22](https://github.com/ARC-907/knowledge-engine/pull/22) | Admin 401 matrix and CORS preflight tests |
| B-010 | Agent board | `GET ?scope=<new>` created a database file | Reads went through the create path | Audit | [#22](https://github.com/ARC-907/knowledge-engine/pull/22) | No-file-created test |
| B-009 | MCP | `search` with `kind` returned fewer than `limit` hits | Kind filter applied after the limit | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Kind-before-limit test |
| B-008 | Agent board | Sweeper lease raised `UnboundLocalError` on corrupt JSON | Variable assigned only on the success path | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Corrupt-lease test |
| B-007 | Tests | One test failed intermittently | Module-level sweeper singleton posted into the test's channel | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Sweeper disabled and reset per test |
| B-006 | Server | Dashboard never mounted at `/ui` | UI path resolved to a missing directory after the layout refactor | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | `/ui/` 200 test |
| B-005 | Core | Default corpus and data paths pointed inside the package | `parents[2]` changed meaning after the layout refactor | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Clean-environment path tests |
| B-004 | Packaging | Three different version numbers in use | Versions hard-coded in several modules | Audit | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Version-equality tests |
| B-003 | Generation | Cloud provider returned a stub string | `CloudHTTPProvider.generate` was never implemented | Audit | [#26](https://github.com/ARC-907/knowledge-engine/pull/26) | Mocked OpenAI-compatible endpoint test |
| B-002 | MCP | `scripts/mcp-client-config.example.json` was invalid JSON | Hand-edited example | Audit | [#24](https://github.com/ARC-907/knowledge-engine/pull/24) | JSON validity test |
| B-001 | Tests | Browser and docs examples referenced nonexistent modules | Stale paths after the layout refactor | Audit | [#31](https://github.com/ARC-907/knowledge-engine/pull/31) | `scripts/check_docs_paths.py` in CI |
