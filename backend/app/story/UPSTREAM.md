# Shiori Story adaptation

Source: [Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent/tree/d3acaed186b172a78a5892e0307e9affd89759f0/plugins/story), pinned `d3acaed186b172a78a5892e0307e9affd89759f0`.
Copyright (c) 2026 YinFengWindy; MIT license retained in `SHIORI-LICENSE.txt`.

The data objects, transactional SQLite repository, time/continuity rules, director prompt/parser and application orchestration are adapted from `plugins/story/backend/`. Meido replaces the SDK, bridge/model activation and plugin image tool with its existing RoleStore, provider adapter, FastAPI and paid-attempt ledger. No Shiori plugin runtime is embedded.

Meido additions: optional text-only presentation; one image per player-triggered turn; existing manual NovelAI limits/credentials; exact request replay; recorded real image receipts; local-only image recovery; owner-safe media URLs; frozen role assets; explicit retry and non-destructive archive/restore. Story never fabricates chat deliveries. Its frozen media remain independent; when the owner enables NovelAI's existing optional role-library writeback, that setting also applies to Story's role-bound generate calls, as upstream does.

Frontend pure helpers in `web/src/features/story/{types,storyPlayback,storyBeatPresentation,storyArchivePresentation,storyCreationWizard,storyTime,selectors}.ts` and type shapes are adapted from the corresponding pinned Story UI sources under the same MIT notice. The page, wizard, settings, game, gallery and HTTP client are Meido-native implementations.
