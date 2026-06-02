"""Config & subentry flows.

Main flow: validate the Anthropic key (``models.list``) and create one entry holding the
key plus a default conversation subentry grounded on ``[assist, cooper]``. Subentry flow:
create/reconfigure an agent (name, persona, APIs, recommended) with an advanced step for
model, caching, tokens, thinking, web search, observe mode, and proactivity.
"""

from __future__ import annotations

from typing import Any

import anthropic
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TemplateSelector,
)

from .const import (
    CONF_CHAT_MODEL,
    CONF_CLEANUP_REVIEW,
    CONF_CONFIRM_BULK_THRESHOLD,
    CONF_HONESTY,
    CONF_HUMOR,
    CONF_LOCATE_NOTIFY,
    CONF_MAX_TOKENS,
    CONF_OBSERVE_MODE,
    CONF_PROACTIVITY,
    CONF_REVIEW_NOTIFY,
    CONF_PROMPT,
    CONF_PROMPT_CACHING,
    CONF_RECOMMENDED,
    CONF_THINKING_BUDGET,
    CONF_WEB_SEARCH,
    CONF_WEB_SEARCH_MAX_USES,
    COOPER_PERSONA_PROMPT,
    DEFAULT,
    DEFAULT_CONVERSATION_NAME,
    DOMAIN,
    ENTRY_TITLE,
    PromptCaching,
    SUBENTRY_TYPE_CONVERSATION,
)
from .provider.anthropic_client import AnthropicProvider

RECOMMENDED_CONVERSATION_OPTIONS = {
    CONF_RECOMMENDED: True,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST, DOMAIN],
    CONF_PROMPT: COOPER_PERSONA_PROMPT,
    CONF_OBSERVE_MODE: True,
    CONF_PROACTIVITY: True,
}


async def _validate_key(hass: Any, api_key: str) -> str | None:
    """Return an error key, or None if the key is valid."""
    provider = AnthropicProvider(hass, api_key)
    try:
        await provider.validate_key()
    except anthropic.AuthenticationError:
        return "invalid_auth"
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return "cannot_connect"
    except anthropic.AnthropicError:
        return "unknown"
    return None


class CooperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Cooper config flow (just the API key)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_API_KEY: user_input[CONF_API_KEY]})
            error = await _validate_key(self.hass, user_input[CONF_API_KEY])
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=ENTRY_TITLE,
                    data={CONF_API_KEY: user_input[CONF_API_KEY]},
                    subentries=[
                        {
                            "subentry_type": SUBENTRY_TYPE_CONVERSATION,
                            "data": RECOMMENDED_CONVERSATION_OPTIONS,
                            "title": DEFAULT_CONVERSATION_NAME,
                            "unique_id": None,
                        }
                    ],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_CONVERSATION: ConversationSubentryFlowHandler}


def _llm_api_options(hass: Any) -> list[SelectOptionDict]:
    return [
        SelectOptionDict(label=api.name, value=api.id)
        for api in llm.async_get_apis(hass)
    ]


class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    """Create or reconfigure one Cooper agent."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    @property
    def _is_reconfigure(self) -> bool:
        return self.source == "reconfigure"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._options = dict(RECOMMENDED_CONVERSATION_OPTIONS)
        self._options[CONF_NAME] = DEFAULT_CONVERSATION_NAME
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        self._options = dict(self._get_reconfigure_subentry().data)
        self._options.setdefault(CONF_NAME, DEFAULT_CONVERSATION_NAME)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            self._options.update(user_input)
            if self._options.get(CONF_RECOMMENDED, True):
                return self._finish()
            return await self.async_step_advanced()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=self._options.get(CONF_NAME, DEFAULT_CONVERSATION_NAME),
                ): str,
                vol.Optional(
                    CONF_PROMPT,
                    default=self._options.get(CONF_PROMPT, COOPER_PERSONA_PROMPT),
                ): TemplateSelector(),
                vol.Optional(
                    CONF_LLM_HASS_API,
                    default=self._options.get(
                        CONF_LLM_HASS_API, [llm.LLM_API_ASSIST, DOMAIN]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_llm_api_options(self.hass), multiple=True
                    )
                ),
                vol.Required(
                    CONF_RECOMMENDED,
                    default=self._options.get(CONF_RECOMMENDED, True),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            self._options.update(user_input)
            return self._finish()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHAT_MODEL,
                    default=self._options.get(
                        CONF_CHAT_MODEL, DEFAULT[CONF_CHAT_MODEL]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=self._model_options(), custom_value=True, sort=True
                    )
                ),
                vol.Required(
                    CONF_PROMPT_CACHING,
                    default=self._options.get(
                        CONF_PROMPT_CACHING, DEFAULT[CONF_PROMPT_CACHING]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[c.value for c in PromptCaching],
                        translation_key=CONF_PROMPT_CACHING,
                    )
                ),
                vol.Required(
                    CONF_MAX_TOKENS,
                    default=self._options.get(CONF_MAX_TOKENS, DEFAULT[CONF_MAX_TOKENS]),
                ): NumberSelector(NumberSelectorConfig(min=100, max=8192, step=1)),
                vol.Required(
                    CONF_THINKING_BUDGET,
                    default=self._options.get(
                        CONF_THINKING_BUDGET, DEFAULT[CONF_THINKING_BUDGET]
                    ),
                ): NumberSelector(NumberSelectorConfig(min=0, max=8192, step=1)),
                vol.Required(
                    CONF_WEB_SEARCH,
                    default=self._options.get(CONF_WEB_SEARCH, DEFAULT[CONF_WEB_SEARCH]),
                ): BooleanSelector(),
                vol.Required(
                    CONF_WEB_SEARCH_MAX_USES,
                    default=self._options.get(
                        CONF_WEB_SEARCH_MAX_USES, DEFAULT[CONF_WEB_SEARCH_MAX_USES]
                    ),
                ): NumberSelector(NumberSelectorConfig(min=1, max=20, step=1)),
                vol.Required(
                    CONF_HUMOR,
                    default=self._options.get(CONF_HUMOR, DEFAULT[CONF_HUMOR]),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%")
                ),
                vol.Required(
                    CONF_HONESTY,
                    default=self._options.get(CONF_HONESTY, DEFAULT[CONF_HONESTY]),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%")
                ),
                vol.Required(
                    CONF_OBSERVE_MODE,
                    default=self._options.get(
                        CONF_OBSERVE_MODE, DEFAULT[CONF_OBSERVE_MODE]
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_PROACTIVITY,
                    default=self._options.get(
                        CONF_PROACTIVITY, DEFAULT[CONF_PROACTIVITY]
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_CLEANUP_REVIEW,
                    default=self._options.get(
                        CONF_CLEANUP_REVIEW, DEFAULT[CONF_CLEANUP_REVIEW]
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_REVIEW_NOTIFY,
                    default=self._options.get(
                        CONF_REVIEW_NOTIFY, DEFAULT[CONF_REVIEW_NOTIFY]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=self._notify_options(),
                        multiple=True,
                        custom_value=True,
                    )
                ),
                vol.Optional(
                    CONF_LOCATE_NOTIFY,
                    default=self._options.get(
                        CONF_LOCATE_NOTIFY, DEFAULT[CONF_LOCATE_NOTIFY]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=self._notify_options(),
                        multiple=True,
                        custom_value=True,
                    )
                ),
                vol.Required(
                    CONF_CONFIRM_BULK_THRESHOLD,
                    default=self._options.get(
                        CONF_CONFIRM_BULK_THRESHOLD,
                        DEFAULT[CONF_CONFIRM_BULK_THRESHOLD],
                    ),
                ): NumberSelector(NumberSelectorConfig(min=1, max=50, step=1)),
            }
        )
        return self.async_show_form(step_id="advanced", data_schema=schema)

    def _notify_options(self) -> list[SelectOptionDict]:
        """Notify targets for the cleanup review: the HA bell + every notify.* service.

        custom_value is on, so a notify group or any target can also be typed.
        """
        options = [
            SelectOptionDict(
                label="Home Assistant notification bell",
                value="persistent_notification.create",
            )
        ]
        for name in sorted(self.hass.services.async_services().get("notify", {})):
            options.append(
                SelectOptionDict(label=f"notify.{name}", value=f"notify.{name}")
            )
        return options

    def _model_options(self) -> list[SelectOptionDict]:
        entry = self._get_entry()
        models = getattr(entry, "runtime_data", None)
        options: list[SelectOptionDict] = []
        if models is not None and getattr(models, "coordinator", None) is not None:
            for model in models.coordinator.data or []:
                options.append(
                    SelectOptionDict(label=model.display_name, value=model.id)
                )
        if not any(o["value"] == DEFAULT[CONF_CHAT_MODEL] for o in options):
            options.append(
                SelectOptionDict(
                    label=str(DEFAULT[CONF_CHAT_MODEL]),
                    value=str(DEFAULT[CONF_CHAT_MODEL]),
                )
            )
        return options

    def _finish(self) -> SubentryFlowResult:
        title = self._options.get(CONF_NAME, DEFAULT_CONVERSATION_NAME)
        if self._is_reconfigure:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=self._options,
                title=title,
            )
        return self.async_create_entry(title=title, data=self._options)
