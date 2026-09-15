# Project working agreements

- A normal code commit or push is not permission to publish a release.
- Create a GitHub Release, publish release assets, or create a release tag only after the user explicitly says to publish. A request to fix, build, test, or continue is not release authorization.
- Keep local trial builds distinct from published installers. Do not overwrite an existing public release or bump the product version merely for an unrequested release.
- Use exactly the release version specified by the user. Never infer or auto-increment it. A release body contains only that version's changes, without copied older version sections.

## Agent and skill routing contract

- Treat the user's current request as the task contract. Platform safety and explicit user instructions take precedence over repository guidance, then the closest `AGENTS.md`, the selected skill, and its references.
- Keep `AGENTS.md` for always-on repository rules. Keep repeatable workflows in `SKILL.md`; load references, scripts, and assets only after the skill is selected and the current step needs them.
- Use the smallest sufficient skill set. Prefer one suite entrypoint for a family of internal modules; keep module IDs and action boundaries intact.
- Before dispatching a subagent, define one bounded objective, owned files or read-only scope, acceptance evidence, dependencies, and a completion window. Dispatch only when independent work saves time or improves confidence.
- A successful child result must include status, changed artifacts, checks actually run, and remaining risks. The primary agent owns integration and final verification.
- A skill can guide execution but cannot grant permission for a new external side effect. If a skill causes a pause or changes the route, identify its exact file and distinguish its requirement from an interpretation.
- Finish reversible, authorized work autonomously. Ask only for a missing decision that materially changes scope, access, or an irreversible action, and ask after the reviewable result is prepared.

## Repo map and checks

- `src/agent_manager/`: Python services and local APIs; `frontend/src/`: React UI; `tests/`: Python integration/runtime coverage; `frontend/src/*.test.js`: frontend behavior tests.
- Backend checks: `python -m pytest -q`. Frontend checks: `npm test -- --runInBand` and `npm run build` from `frontend/`.
- Prefer the smallest relevant check first. After a cross-module or configuration change, run the broader suite and report the actual command and result.
