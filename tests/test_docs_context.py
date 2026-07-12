from shared.docs_context import load_project_context, topic_context_files


def test_load_project_context_reads_existing_files(tmp_path):
    (tmp_path / "BACKLOG.md").write_text("# Backlog\nзадача 1", encoding="utf-8")
    (tmp_path / "AI_CHANGELOG.md").write_text("\n".join(f"строка {i}" for i in range(500)), encoding="utf-8")

    context = load_project_context(str(tmp_path))

    assert "BACKLOG.md" in context
    assert "задача 1" in context
    assert "AI_CHANGELOG.md" in context
    assert "строка 0" in context
    assert "строка 499" not in context  # обрезано хвостом за пределами _CHANGELOG_TAIL_LINES


def test_load_project_context_missing_files_returns_empty(tmp_path):
    assert load_project_context(str(tmp_path)) == ""


def test_load_project_context_respects_max_chars(tmp_path):
    (tmp_path / "RELEASE_NOTES.md").write_text("x" * 5000, encoding="utf-8")

    context = load_project_context(str(tmp_path), max_chars=100)

    assert len(context) <= 120  # 100 + "...(обрезано)" suffix


def test_load_project_context_labels_section_with_repo_name(tmp_path):
    repo_dir = tmp_path / "SomeRepo"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "BACKLOG.md").write_text("контент", encoding="utf-8")

    context = load_project_context(str(docs_dir))

    assert "[SomeRepo] BACKLOG.md" in context


def test_load_project_context_accepts_multiple_paths(tmp_path):
    repo_a = tmp_path / "RepoA" / "docs"
    repo_b = tmp_path / "RepoB" / "docs"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    (repo_a / "BACKLOG.md").write_text("из репо А", encoding="utf-8")
    (repo_b / "ARCHITECTURE.md").write_text("из репо Б", encoding="utf-8")

    context = load_project_context([str(repo_a), str(repo_b)])

    assert "[RepoA] BACKLOG.md" in context
    assert "из репо А" in context
    assert "[RepoB] ARCHITECTURE.md" in context
    assert "из репо Б" in context


def test_load_project_context_one_huge_file_does_not_starve_others(tmp_path):
    # Регрессия: BACKLOG.md в реальном FinAssist весит ~500 КБ — при наивной
    # "конкатенировать всё, потом обрезать в конце" стратегии он один съедал
    # весь бюджет max_chars, и AI_CHANGELOG.md/второй репозиторий не попадали
    # в контекст вообще. Бюджет должен делиться МЕЖДУ файлами.
    (tmp_path / "BACKLOG.md").write_text("а" * 50_000, encoding="utf-8")
    (tmp_path / "AI_CHANGELOG.md").write_text("свежая запись из чейнджлога", encoding="utf-8")

    context = load_project_context(str(tmp_path), max_chars=4000)

    assert "AI_CHANGELOG.md" in context
    assert "свежая запись из чейнджлога" in context


def test_load_project_context_includes_extra_filenames_beyond_core_list(tmp_path):
    (tmp_path / "BACKLOG.md").write_text("бэклог", encoding="utf-8")
    (tmp_path / "ONBOARDING_FLOW.md").write_text("детали онбординга", encoding="utf-8")

    context = load_project_context(str(tmp_path), extra_filenames=["ONBOARDING_FLOW.md"])

    assert "ONBOARDING_FLOW.md" in context
    assert "детали онбординга" in context


def test_load_project_context_gives_extra_filenames_bigger_budget(tmp_path):
    # extra_filenames — файлы, явно релевантные вопросу, должны получать
    # вдвое больший бюджет, чем базовый "на всякий случай" набор, иначе
    # добавление тематических файлов просто размывает бюджет без пользы.
    (tmp_path / "BACKLOG.md").write_text("б" * 10_000, encoding="utf-8")
    (tmp_path / "ONBOARDING_FLOW.md").write_text("о" * 10_000, encoding="utf-8")

    context = load_project_context(str(tmp_path), max_chars=3000, extra_filenames=["ONBOARDING_FLOW.md"])

    backlog_section = context.split("## [")[1]
    onboarding_section = context.split("## [")[2]
    assert len(onboarding_section) > len(backlog_section)


def test_topic_context_files_matches_keyword():
    assert "ONBOARDING_FLOW.md" in topic_context_files("как устроен онбординг новых пользователей?")
    assert "PAYCHECK_FEATURE.md" in topic_context_files("что происходит при получке?")


def test_topic_context_files_no_match_returns_empty():
    assert topic_context_files("привет, как дела?") == []


def test_topic_context_files_no_duplicates_across_keywords():
    # "цел" и "goal" мапятся на один и тот же набор файлов — вопрос,
    # содержащий оба слова, не должен задваивать имена.
    files = topic_context_files("расскажи про goals и цели одновременно")
    assert files.count("GOALS_SCREEN.md") == 1
