# Project working agreements

- A normal code commit or push is not permission to publish a release.
- Create a GitHub Release, publish release assets, or create a release tag only after the user explicitly says to publish. A request to fix, build, test, or continue is not release authorization.
- Keep local trial builds distinct from published installers. Do not overwrite an existing public release or bump the product version merely for an unrequested release.
- Use exactly the release version specified by the user. Never infer or auto-increment it. A release body contains only that version's changes, without copied older version sections.
