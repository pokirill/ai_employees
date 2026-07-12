from __future__ import annotations

import subprocess
from pathlib import Path

# "Базовый" контекст — грузится всегда, для любого вопроса. Не весь Docs/,
# там есть многотысячестрочные исторические логи и build-специфичные task
# card'ы, не полезные для общих вопросов. FinAssist и Finik-backend называют
# файлы по-разному (FinAssist: RELEASE_NOTES.md/ARCHITECTURE_DEEP_DIVE.md;
# Finik-backend: API_CONTRACT.md/ARCHITECTURE.md) — список общий, берём то,
# что реально существует в каждом репо.
_CANDIDATE_CONTEXT_FILES = [
    "BACKLOG.md",
    "AI_CHANGELOG.md",
    "RELEASE_NOTES.md",
    "API_CONTRACT.md",
    "ARCHITECTURE.md",
    "ARCHITECTURE_DEEP_DIVE.md",
]

# "Тематический" контекст — подключается ТОЛЬКО когда вопрос явно об этой
# теме (см. topic_context_files), не на каждый вопрос: у FinAssist/Docs
# полтора десятка содержательных файлов (бизнес-логика, онбординг, цели,
# зарплата, аналитика...), но тащить их все в промпт на любой вопрос — и
# дорого по токенам, и размывает бюджет между файлами до бесполезного
# огрызка на каждый (см. per_file_budget в load_project_context).
_TOPIC_CONTEXT_FILES: dict[str, list[str]] = {
    "онбординг": ["ONBOARDING_FLOW.md", "ONBOARDING_GROWTH_PLAN.md"],
    "onboarding": ["ONBOARDING_FLOW.md", "ONBOARDING_GROWTH_PLAN.md"],
    "цел": ["GOALS_SCREEN.md", "SEMANTICS_GOALS.md", "GOALS_AGENT_GAP_ANALYSIS.md"],
    "goal": ["GOALS_SCREEN.md", "SEMANTICS_GOALS.md", "GOALS_AGENT_GAP_ANALYSIS.md"],
    "зарплат": ["PAYCHECK_FEATURE.md", "PAYCHECK_REQUIREMENTS.md", "PAYCHECK_IMPROVEMENTS_PLAN.md"],
    "получк": ["PAYCHECK_FEATURE.md", "PAYCHECK_REQUIREMENTS.md", "PAYCHECK_IMPROVEMENTS_PLAN.md"],
    "paycheck": ["PAYCHECK_FEATURE.md", "PAYCHECK_REQUIREMENTS.md", "PAYCHECK_IMPROVEMENTS_PLAN.md"],
    "бизнес-логик": ["BUSINESS_LOGIC.md"],
    "business logic": ["BUSINESS_LOGIC.md"],
    "аналитик": ["ANALYTICS_SPEC.md"],
    "analytics": ["ANALYTICS_SPEC.md"],
    "метрик": ["ANALYTICS_SPEC.md"],
    "retention": ["RETENTION_ROADMAP.md"],
    "удержан": ["RETENTION_ROADMAP.md"],
    "отток": ["RETENTION_ROADMAP.md"],
    "прогноз": ["FORECAST_DATE_LOGIC.md"],
    "forecast": ["FORECAST_DATE_LOGIC.md"],
    "кэшфлоу": ["CASHFLOW_AWARE_DESIGN.md"],
    "cashflow": ["CASHFLOW_AWARE_DESIGN.md"],
    "подушк": ["HOLD_GOALS_DEPOSITS.md"],
    "депозит": ["HOLD_GOALS_DEPOSITS.md"],
    "onboarding_qa": ["BACKEND_ONBOARDING_QA.md"],
}

# Сколько последних строк AI_CHANGELOG.md брать — файл растёт неограниченно,
# и только «голова» (самые свежие записи) релевантна для контекста.
_CHANGELOG_TAIL_LINES = 400


def topic_context_files(question: str) -> list[str]:
    """Имена файлов, релевантные конкретному вопросу (сверх базового набора),
    по ключевым словам — см. _TOPIC_CONTEXT_FILES. Без дублей, порядок
    сохраняется по первому совпадению."""
    normalized = question.lower()
    seen: set[str] = set()
    result: list[str] = []
    for keyword, filenames in _TOPIC_CONTEXT_FILES.items():
        if keyword not in normalized:
            continue
        for filename in filenames:
            if filename not in seen:
                seen.add(filename)
                result.append(filename)
    return result


def sync_docs_repo(docs_path: str) -> None:
    """git pull, если docs_path — рабочая копия git-репозитория. Тихо
    пропускает, если это не git или сеть недоступна — старые доки лучше,
    чем упавший бот."""
    repo_root = Path(docs_path).parent
    if not (repo_root / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def sync_docs_repos(docs_paths: list[str]) -> None:
    for path in docs_paths:
        sync_docs_repo(path)


def load_project_context(
    docs_paths: list[str] | str, max_chars: int = 12_000, extra_filenames: list[str] | None = None
) -> str:
    """Собирает контекст из Docs/*.md одного или нескольких репозиториев
    (FinAssist, Finik-backend) для промпта LLM. Секции подписаны именем
    репозитория, чтобы ассистент понимал, откуда какой факт.

    extra_filenames (см. topic_context_files) — файлы, релевантные конкретно
    заданному вопросу; получают вдвое больший бюджет, чем базовые "на любой
    случай" файлы, — раз уж их явно выбрали как релевантные, для них важнее
    реальное содержание, чем для общего фона.

    Бюджет max_chars делится между файлами пропорционально их весу, а не
    расходуется последовательно — иначе один огромный файл (в FinAssist
    BACKLOG.md весит ~500 КБ) съедает весь бюджет целиком, и ассистент
    никогда не увидит ни AI_CHANGELOG.md, ни второй репозиторий вообще.
    """
    if isinstance(docs_paths, str):
        docs_paths = [docs_paths]

    all_filenames = list(_CANDIDATE_CONTEXT_FILES)
    priority_filenames = set(extra_filenames or [])
    for filename in priority_filenames:
        if filename not in all_filenames:
            all_filenames.append(filename)

    raw_entries: list[tuple[str, str, str, int]] = []  # (repo_label, filename, text, weight)
    for docs_path in docs_paths:
        base = Path(docs_path)
        repo_label = base.parent.name or str(base)
        for filename in all_filenames:
            file_path = base / filename
            if not file_path.exists():
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            if filename == "AI_CHANGELOG.md":
                lines = text.splitlines()
                text = "\n".join(lines[:_CHANGELOG_TAIL_LINES])
            weight = 2 if filename in priority_filenames else 1
            raw_entries.append((repo_label, filename, text, weight))

    if not raw_entries:
        return ""

    total_weight = sum(weight for *_rest, weight in raw_entries)
    unit_budget = max(max_chars // total_weight, 500)
    sections: list[str] = []
    for repo_label, filename, text, weight in raw_entries:
        budget = unit_budget * weight
        if len(text) > budget:
            text = text[:budget] + "\n...(обрезано)"
        sections.append(f"## [{repo_label}] {filename}\n{text}")

    combined = "\n\n".join(sections)
    if len(combined) > max_chars:
        # Заголовки секций + floor-минимум (500 симв./файл) в сумме могут
        # чуть превысить max_chars — раньше здесь была слепая обрезка ХВОСТА
        # целиком, а это систематически вырезало последние секции целиком
        # (второй репозиторий всегда обрабатывается последним, значит терял
        # свои файлы первым). Вместо этого урезаем ПРОПОРЦИОНАЛЬНО каждую
        # секцию — каждый файл теряет свою долю превышения, но не исчезает
        # целиком только из-за порядка обработки.
        scale = max_chars / len(combined)
        sections = [section[: max(int(len(section) * scale), 1)] for section in sections]
        combined = "\n\n".join(sections)
    return combined
