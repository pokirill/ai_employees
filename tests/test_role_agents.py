from shared.role_agents import ROLE_AGENTS, list_role_agents, role_agent_for_command


def test_role_agents_registry_has_five_roles():
    # Сознательно не весь каталог ролей плейбука Авито — см. ORG_STRUCTURE.md.
    assert set(ROLE_AGENTS.keys()) == {"dev", "techlead", "qa", "design", "product"}


def test_role_agent_for_command_returns_matching_role():
    role = role_agent_for_command("techlead")
    assert role is not None
    assert role.display_name == "Тимлид"
    assert "techlead-profile.md" in role.playbook_files


def test_role_agent_for_command_unknown_returns_none():
    assert role_agent_for_command("nonexistent") is None


def test_every_role_has_non_empty_persona_and_playbook_files():
    for role in list_role_agents():
        assert role.persona_prompt.strip()
        assert len(role.playbook_files) >= 1


def test_list_role_agents_matches_registry_size():
    assert len(list_role_agents()) == len(ROLE_AGENTS)


def test_role_command_matches_dict_key():
    # _make_role_handler в team_bot/main.py регистрирует aiogram Command по
    # role.command — если он разойдётся с ключом словаря, /roles покажет
    # одно, а реально сработает другое.
    for key, role in ROLE_AGENTS.items():
        assert role.command == key
        assert role.key == key
