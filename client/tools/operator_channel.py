"""MCP tools for talking to the operator through the web console.

Registered only by ``amadeus-web`` (the stdio ``amadeus-server`` has no
browser to talk to). These calls bypass the action queue: they never touch
the instrument, and a long ``wait_for_operator`` must not hold up the
operator's own blocks.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

OPERATOR_TOOLS = frozenset({"wait_for_operator", "ask_operator", "tell_operator"})


def register(mcp: FastMCP, channel) -> None:

    @mcp.tool()
    async def wait_for_operator(timeout_s: float = 300.0) -> dict:
        """Wait for the next instruction the operator types in the web console.

        Call this whenever you have finished a task and have nothing else to
        do, and call it again after it times out: it is how the operator
        gives you work. Instructions that arrive while you are busy are also
        appended to your next tool result as ``operator_message``.

        Args:
            timeout_s: How long to wait before returning with no instructions.

        Returns:
            ``instructions``: list of messages (oldest first), and ``timed_out``.
        """
        entries = await channel.wait_for_instructions(timeout_s)
        return {"instructions": [e.text for e in entries], "timed_out": not entries}

    @mcp.tool()
    async def ask_operator(question: str, options: list[str] | None = None,
                           timeout_s: float = 900.0) -> dict:
        """Ask the operator a question in the web console and wait for the answer.

        Use this for decisions that are the operator's to make (switching
        cameras, opening valves, which particle to study) instead of guessing.

        Args:
            question: The question, self-contained.
            options: Optional answer buttons to offer; the operator may still
                type a free answer.
            timeout_s: Give up after this long (raises an error).

        Returns:
            ``answer``: the operator's reply text.
        """
        return {"answer": await channel.ask(question, options, timeout_s)}

    @mcp.tool()
    async def tell_operator(message: str) -> dict:
        """Post a short status line to the operator's web console terminal.

        Args:
            message: What you are doing or have found, one or two sentences.
        """
        entry = channel.tell(message)
        return {"posted": entry.id}
