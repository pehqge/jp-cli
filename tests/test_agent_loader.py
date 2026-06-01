from jp import agent_loader


def test_bootstrap_embeds_agent_source_and_registers_comm():
    code = agent_loader.build_bootstrap(root="users/alice/proj", target="jp.fs")
    assert "class Agent" in code  # the agent source is embedded
    assert "get_ipython().kernel.comm_manager.register_target" in code
    assert '"jp.fs"' in code or "'jp.fs'" in code
    assert "users/alice/proj" in code  # the jailed root is baked in


def test_bootstrap_is_valid_python():
    code = agent_loader.build_bootstrap(root="users/alice/proj", target="jp.fs")
    compile(code, "<bootstrap>", "exec")  # must parse


def test_bootstrap_root_is_safely_quoted():
    # A crafted root must not be able to break out of the string literal.
    code = agent_loader.build_bootstrap(root='a"; import os; os.system("x")  #', target="jp.fs")
    compile(code, "<bootstrap>", "exec")  # still valid (repr-quoted), no injection
    assert 'os.system("x")' not in code.replace(repr('a"; import os; os.system("x")  #'), "")
