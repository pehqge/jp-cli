"""Build the one-shot ``execute_request`` code that boots the agent in a kernel.

We embed the agent SOURCE TEXT (not a pickle, not a download) and append a tiny
bootstrap that instantiates :class:`jp._agent.agentd.Agent` under the jailed root
and registers a Jupyter ``comm`` target. The comm handler decodes an ``fsrpc``
request from ``msg['content']['data']`` and replies via ``comm.send(resp,
buffers=[...])`` -- raw bytes ride the comm's binary buffers (no base64).

Security: ``root`` and ``target`` are inserted with ``repr()`` so a hostile or
odd value cannot break out of the string literal into executable code.
"""

from __future__ import annotations

from importlib.resources import files

# Read the embedded agent source via importlib.resources so it works both from a
# normal install AND from a zipapp (jp.pyz), where the package lives inside a zip
# and a filesystem Path(__file__) read would raise NotADirectoryError.
_AGENT_SRC = (files("jp._agent") / "agentd.py").read_text(encoding="utf-8")


def build_bootstrap(*, root: str, target: str = "jp.fs", writable: bool = False) -> str:
    register = (
        "\n# --- jp agent bootstrap ---\n"
        f"def _jp_register():\n"
        f"    _agent = Agent({root!r}, writable={writable!r})\n"
        f"\n"
        f"    def _target(comm, open_msg):\n"
        f"        @comm.on_msg\n"
        f"        def _on_msg(msg):\n"
        f'            req = msg["content"]["data"]\n'
        f'            resp, buffers = _agent.handle(req, msg.get("buffers"))\n'
        f"            comm.send(resp, buffers=buffers)\n"
        f"\n"
        f"    get_ipython().kernel.comm_manager.register_target({target!r}, _target)\n"
        f"\n"
        f"_jp_register()\n"
    )
    return _AGENT_SRC + register
