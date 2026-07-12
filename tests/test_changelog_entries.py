from channel_bot.changelog_entries import (
    load_used_titles,
    mark_title_used,
    next_unused_entry,
    parse_changelog_entries,
)

_SAMPLE_CHANGELOG = """\
- **[2026-07-10: Второй заголовок]**
  - Текст второй записи, самой свежей.
  - Ещё строка.

- **[2026-07-09: Первый заголовок]**
  - Текст первой записи.
"""


def test_parse_changelog_entries_splits_by_header(tmp_path):
    changelog = tmp_path / "AI_CHANGELOG.md"
    changelog.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")

    entries = parse_changelog_entries(str(changelog))

    assert len(entries) == 2
    assert entries[0]["title"] == "2026-07-10: Второй заголовок"
    assert "самой свежей" in entries[0]["body"]
    assert entries[1]["title"] == "2026-07-09: Первый заголовок"


def test_parse_changelog_entries_missing_file_returns_empty(tmp_path):
    assert parse_changelog_entries(str(tmp_path / "missing.md")) == []


def test_next_unused_entry_skips_marked_titles(tmp_path):
    changelog = tmp_path / "AI_CHANGELOG.md"
    changelog.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    state = tmp_path / "used.json"

    mark_title_used(str(state), "2026-07-10: Второй заголовок")
    entry = next_unused_entry(str(changelog), str(state))

    assert entry is not None
    assert entry["title"] == "2026-07-09: Первый заголовок"


def test_next_unused_entry_all_used_returns_none(tmp_path):
    changelog = tmp_path / "AI_CHANGELOG.md"
    changelog.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    state = tmp_path / "used.json"

    for entry in parse_changelog_entries(str(changelog)):
        mark_title_used(str(state), entry["title"])

    assert next_unused_entry(str(changelog), str(state)) is None


def test_load_used_titles_missing_file_returns_empty_set(tmp_path):
    assert load_used_titles(str(tmp_path / "missing.json")) == set()
