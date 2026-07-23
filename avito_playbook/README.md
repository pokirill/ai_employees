# Avito Engineering Playbook (vendored)

Source: https://github.com/avito-tech/playbook (public, no LICENSE file — vendored
here for internal `team_bot` assistant context only, not redistributed).

`docs/` is a snapshot of the repo's `.md` files, copied as-is (not a submodule —
matches how FinAssist/Finik-backend docs are read by `shared/docs_context.py`:
a local path, no live git dependency). To refresh, re-clone the source repo and
copy its `*.md` files over `docs/`.

Used by `team_bot` (see `shared/config.py`'s `TeamBotConfig.avito_playbook_path`
and `shared/docs_context.py`) as a third context root alongside FinAssist/Finik-backend
docs, per Kirill's request that the assistant "придерживается такого плейбука."
