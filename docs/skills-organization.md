# Skills organization audit

Updated: 2026-09-15

## What changed

- The toolbox now treats a skill family as one entry and shows its `SKILL.md` modules inside an expandable group. Each module keeps its own ID, fingerprint, path, and enable/disable/delete action.
- The inventory API emits stable family metadata (`skillRoot`, `skillRootName`, `rootPath`, `groupKey`) so the UI does not need to infer relationships from display text.
- PaperSpine internal modules are explicit-only in `agents/openai.yaml`. The `paper-spine` entry remains the automatic suite entrypoint; direct `$paper-spine-*` calls continue to work.

## Current families

| Family | Entries | Decision |
| --- | ---: | --- |
| PaperSpine | 12 | Keep the suite entrypoint visible; keep modules available behind expansion and explicit invocation. |
| Nature writing/research tools | 11 | Keep separate. These are distinct deliverables with different evidence and output contracts. |
| Research tools | 6 | Keep separate. `research-hub-multi-ai` is conditional and should only activate for multi-delegate research-hub work. |
| Academic writing tools | 7 | Keep separate. `academic-pipeline` and `paper-spine` are different end-to-end workflows. |
| Design/UI tools | 4 | Keep separate. They overlap in topic but use different output stacks and constraints. |
| Standalone tools | 21 | Keep available; no safe automatic merge was found. |

## Candidates to disable when not in use

These are recommendations, not automatic deletions:

1. `paper-framework-figure-studio-pro` — already disabled in `config.toml`; it is asset-heavy and only needed for the specialized figure workflow.
2. `paper-spine-*` modules — now explicit-only, so they do not need a separate global enable/disable entry for normal use.
3. `research-hub-multi-ai` — keep disabled unless a research-hub workflow actually needs more than one delegate.
4. `notebooklm-brief-verifier` — keep disabled if NotebookLM briefs are not part of the current workflow.
5. `notion-research-documentation` — keep disabled if Notion is not connected.
6. `collaborating-with-claude` and `collaborating-with-gemini` — keep disabled unless those CLIs are installed and intentionally used.
7. `Visiomaster` — keep disabled unless editable Visio output is required.

No skill directory was deleted, and no user-authored skill content was silently merged. Disable or remove a candidate only after confirming it is not used by an active workflow.

## Audit notes

- A skill family is a presentation/routing concept; the filesystem directory remains the mutation boundary.
- A module description should state whether it is an entrypoint or an internal step. Internal steps should not use broad automatic trigger language.
- Supporting references and assets remain inside their owning skill. The toolbox should show counts and paths, while loading detailed references only when the selected module needs them.

## Routing and installation design basis

The current design follows OpenAI's progressive-disclosure model: metadata is used for discovery, one selected `SKILL.md` is loaded for the workflow, and references or scripts are read only when needed. `AGENTS.md` remains the always-on instruction layer. See the [official skills guide](https://developers.openai.com/codex/skills), [AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md), [multi-agent guide](https://developers.openai.com/codex/multi-agent), and [prompting guide](https://developers.openai.com/codex/prompting).

Community implementations reinforce the same separation: [OpenHands extensions](https://github.com/OpenHands/extensions/blob/main/AGENTS.md) keeps repository rules separate from trigger-loaded skills, while [Cline's command workflow](https://github.com/cline/cline/blob/main/docs/core-workflows/using-commands.mdx) uses explicit commands for deep planning and reusable skills. These are references for structure only; Codex's official runtime remains authoritative.

The toolbox installer continues to require a verified App Server marketplace identity before it calls `plugin/install`. A catalog entry without a verifiable install route stays browse-only. Local deletion remains fingerprint-checked and recoverable through the existing quarantine path.

The local skill audit also normalizes YAML frontmatter: folded descriptions are read correctly, unsupported top-level metadata is removed from discovery headers, and descriptions are kept within the catalog budget. All 61 user skill directories now pass the bundled skill validator.

The existing default approval mode was preserved to avoid changing the user's current execution behavior. A safer interactive profile is available at `C:\Users\19421\.codex\interactive.config.toml`; select it with `codex --profile interactive` when local edits should require approval and network access should stay off.
