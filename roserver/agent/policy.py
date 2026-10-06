"""Default safety policy for natural-language robot operations."""

from __future__ import annotations

from roboagent.tool import (
    ToolDecision,
    ToolEffectKind,
    ToolExecutionPolicy,
    ToolPolicyDecision,
)


class RequireApprovalForSideEffects(ToolExecutionPolicy):
    """Require an operator decision before a tool can affect the robot."""

    async def evaluate(
        self, call: object, tool: object | None, context: object
    ) -> ToolPolicyDecision:
        del call, context
        if getattr(tool, "effect_kind", None) is ToolEffectKind.SIDE_EFFECTING:
            return ToolPolicyDecision(
                ToolDecision.REQUIRE_APPROVAL,
                "Robot-affecting action requires operator approval.",
            )
        return ToolPolicyDecision(ToolDecision.ALLOW)


__all__ = ["RequireApprovalForSideEffects"]
