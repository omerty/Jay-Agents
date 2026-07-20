from src.prompts import get_prompt_settings, prompts_path, save_prompt_settings


def test_get_prompt_settings_includes_templates():
    p = get_prompt_settings("woodway")
    assert p["agent"] == "woodway"
    assert "qualify_system" in p["values"]
    assert "EviData" in p["templates"]["qualify_user"]
    assert "VALUE PROPS" in p["templates"]["outreach_user"]


def test_save_and_reset_prompts(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents" / "woodway"
    agents_dir.mkdir(parents=True)
    (agents_dir / "config.yaml").write_text(
        "name: woodway\ncompany: Test Co\nproduct: Prod\ntagline: Tag\n"
        "icp:\n  industries: [tech]\n  titles: [CEO]\n  company_size: large\n"
    )

    import src.agent as agent_mod
    import src.prompts as prompts_mod

    monkeypatch.setattr(agent_mod, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(prompts_mod, "AGENTS_DIR", tmp_path / "agents")

    saved = save_prompt_settings("woodway", {
        "qualify_extra": "Focus on privacy teams.",
        "outreach_system": "Write short emails only.",
    })
    assert saved["values"]["qualify_extra"] == "Focus on privacy teams."
    assert prompts_path("woodway").exists()

    reset = save_prompt_settings("woodway", {
        "qualify_system": "",
        "qualify_extra": "",
        "outreach_system": "",
        "outreach_extra": "",
    })
    assert not prompts_path("woodway").exists()
    assert reset["values"]["qualify_extra"] == ""
