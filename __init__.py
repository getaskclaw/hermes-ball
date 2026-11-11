"""Ball — shows which of your AI chats are waiting on you, and what each one asked."""
from __future__ import annotations

if __package__:  # Hermes loads this dir as a package; pytest's rootdir collection may not
    from .core import Ball
    from .store import Store

__all__ = ["register", "Ball", "Store"]

HOOKS = ("pre_gateway_dispatch", "post_llm_call", "on_human_input_request",
         "on_human_input_resolved", "agent_loop_stopped")


def register(ctx):
    ball = Ball(Store(), ctx)
    for name in HOOKS:
        ctx.register_hook(name, getattr(ball, name if name.startswith("on_") else "on_" + name))
    ctx.register_command("ball", ball.command,
                         description="Which AI chats are waiting on you? `/ball`, `/ball all`, `/ball more`, `/ball forget`, `/ball help`",
                         args_hint="[all|more|done|forget|help] [hide]")
    return ball
