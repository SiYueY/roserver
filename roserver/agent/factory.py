"""AgentFactory: compose the single roboagent ``Agent`` used by roserver."""

from __future__ import annotations

from typing import Callable, Sequence

from roboagent import Agent
from roboagent.context import PromptInput
from roboagent.model import Model, ModelCapabilities, ModelSettings, create_model
from roboagent.tool import ApprovalProvider, Tool, ToolExecutionPolicy, ToolRegistry

from ..config import Settings
from .approval import ProductApprovalProvider

ModelFactory = Callable[[], Model]


class EchoModel:
    """Deterministic offline model used for local development and smoke tests.

    It is not a substitute for a real Provider; it exists so that
    ``ROSERVER_MODEL=echo`` can boot roserver without network access.
    """

    def __init__(self, reply: str = "Echo: no model provider is configured.") -> None:
        self.reply = reply
        self._capabilities = ModelCapabilities(tool_calling=False)

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    async def _stream(self, context: object, settings: ModelSettings | None):
        from roboagent.model import (
            ModelResponse,
            ResponseCompleted,
            ResponseStarted,
            TextDelta,
        )
        from roboagent.message import AssistantMessage, TextContent

        del settings
        yield ResponseStarted("echo")
        yield TextDelta(1, self.reply)
        yield ResponseCompleted(
            2, ModelResponse(AssistantMessage((TextContent(self.reply),)), _STOP)
        )

    def stream(self, context: object, settings: ModelSettings | None = None):
        return self._stream(context, settings)


def _stop_reason():
    from roboagent.model import FinishReason

    return FinishReason.STOP


_STOP = _stop_reason()


class AgentFactory:
    def __init__(
        self,
        *,
        settings: Settings,
        approval_provider: ProductApprovalProvider | ApprovalProvider | None = None,
        model: Model | None = None,
        model_factory: ModelFactory | None = None,
        tools: Sequence[Tool] = (),
        tool_policy: ToolExecutionPolicy | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.approval_provider = approval_provider
        self._model = model
        self._model_factory = model_factory
        self._tools = tuple(tools)
        self._tool_policy = tool_policy
        self._tool_registry = tool_registry

    def build_model(self) -> Model:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            return self._model_factory()
        return self._model_from_settings()

    def _model_from_settings(self) -> Model:
        name = self.settings.model
        if name == "echo":
            return EchoModel()
        if self.settings.model_config_path is not None:
            from roboagent.config import AppConfig

            app_config = AppConfig.from_yaml(self.settings.model_config_path)
            registry = app_config.to_model_registry()
            model_name = self.settings.model_name or app_config.default_model
            return create_model(model_name, registry=registry)
        raise RuntimeError(
            "No model configured for roserver. Pass model=/model_factory= to "
            "create_app() or set ROSERVER_MODEL=echo / ROSERVER_MODEL_CONFIG."
        )

    def build_registry(self) -> ToolRegistry:
        if self._tool_registry is not None:
            return self._tool_registry
        registry = ToolRegistry()
        for tool in self._tools:
            registry.register(tool)
        return registry

    def build_agent(self) -> Agent:
        prompt = (
            PromptInput(system=self.settings.system_prompt)
            if self.settings.system_prompt
            else None
        )
        return Agent(
            self.build_model(),
            tool_registry=self.build_registry(),
            prompt=prompt,
            tool_policy=self._tool_policy,
            approval_provider=self.approval_provider,
        )


__all__ = ["AgentFactory", "EchoModel", "ModelFactory"]
