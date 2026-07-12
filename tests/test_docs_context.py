from shared.docs_context import load_project_context


def test_load_project_context_reads_existing_files(tmp_path):
    (tmp_path / "BACKLOG.md").write_text("# Backlog\nзадача 1", encoding="utf-8")
    (tmp_path / "AI_CHANGELOG.md").write_text("\n".join(f"строка {i}" for i in range(500)), encoding="utf-8")

    context = load_project_context(str(tmp_path))

    assert "## BACKLOG.md" in context
    assert "задача 1" in context
    assert "## AI_CHANGELOG.md" in context
    assert "строка 0" in context
    assert "строка 499" not in context  # обрезано хвостом за пределами _CHANGELOG_TAIL_LINES


def test_load_project_context_missing_files_returns_empty(tmp_path):
    assert load_project_context(str(tmp_path)) == ""


def test_load_project_context_respects_max_chars(tmp_path):
    (tmp_path / "RELEASE_NOTES.md").write_text("x" * 5000, encoding="utf-8")

    context = load_project_context(str(tmp_path), max_chars=100)

    assert len(context) <= 120  # 100 + "...(обрезано)" suffix
