# Changelog

## 0.25.5

- Fix: `list_automations` with a `name` filter now matches the automation's whole config
  (entity ids, description, triggers, conditions, actions) and entity_id, not only the alias.
  Searching for e.g. "sprinkler" or "deer" finds automations whose alias doesn't contain it.
