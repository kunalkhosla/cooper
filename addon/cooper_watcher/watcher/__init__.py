"""Cooper Watcher: an optional, key-less trigger source for proactivity.

Single invariant: this add-on never controls the home and never talks to Anthropic. The
only write it performs is calling the ``cooper.proactive_check`` service, so all Claude
calls, guardrails, exposure, and audit stay in the integration.
"""
