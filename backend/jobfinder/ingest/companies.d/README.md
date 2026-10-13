Per-sector seed files. Every `*.yaml` here is loaded after `../companies.yaml` (sorted by name).
Format: `companies: [{name, ats, slug, prestige?, size?, industry?, sponsors_visas?}]`.
Rows with an `(ats, slug)` already seen are skipped.
