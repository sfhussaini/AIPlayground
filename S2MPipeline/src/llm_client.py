"""Single source of truth for LLM access."""
from typing import Any, List, Optional, Type

from pydantic import BaseModel
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent

from config import PipelineConfig


class LLMClient:
    """Wraps LangChain model construction so stages don't touch ChatOpenAI."""

    def __init__(self, config: PipelineConfig):
        self._config = config

    def chat_model(
        self,
        temperature: float,
        timeout: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> ChatOpenAI:
        kwargs: dict = dict(
            model=self._config.model_name,
            base_url=self._config.lm_studio_url,
            api_key=self._config.api_key,
            temperature=temperature,
            timeout=timeout or self._config.default_timeout,
        )
        effort = reasoning_effort if reasoning_effort is not None else self._config.reasoning_effort
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        return ChatOpenAI(**kwargs)

    def structured(
        self,
        schema: Type[BaseModel],
        temperature: float,
        timeout: Optional[int] = None,
    ):
        return self.chat_model(temperature, timeout).with_structured_output(schema)

    # ------------------------------------------------------------------
    def vision_chat_model(
        self,
        temperature: float,
        timeout: Optional[int] = None,
    ) -> ChatOpenAI:
        """Same wiring as chat_model, but pointed at config.vision_model_name."""
        return ChatOpenAI(
            model=self._config.vision_model_name,
            base_url=self._config.lm_studio_url,
            api_key=self._config.api_key,
            temperature=temperature,
            timeout=timeout or self._config.default_timeout,
        )

    def vision_structured(
        self,
        schema: Type[BaseModel],
        temperature: float,
        timeout: Optional[int] = None,
    ):
        return self.vision_chat_model(temperature, timeout).with_structured_output(schema)

    # ------------------------------------------------------------------
    def pose_structured(
        self,
        schema: Type[BaseModel],
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ):
        """Pose creation: own model/temperature knobs (config.pose_*).
        The pose LLM never sees the story — only bare action labels."""
        return ChatOpenAI(
            model=self._config.pose_model_name,
            base_url=self._config.lm_studio_url,
            api_key=self._config.api_key,
            temperature=(temperature if temperature is not None
                         else self._config.pose_temperature),
            timeout=timeout or self._config.default_timeout,
        ).with_structured_output(schema)

    # ------------------------------------------------------------------
    def agent(
        self,
        system_prompt: str,
        tools: Optional[List[Any]] = None,
        debug: bool = True,
    ):
        return create_agent(
            model=self.chat_model(0.7),
            tools=tools or [],
            system_prompt=system_prompt,
            debug=debug,
        )