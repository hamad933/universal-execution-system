# UES Provider Ingestion R01

This bounded candidate closes the live-observation gap between the UES watchdog and Jules provider state.

It adds a read-only, paginated Jules inventory path that:
- enumerates current Jules sessions using the runtime `JULES_API_KEY` without mutation;
- proves project ownership only from explicit Jules source/repository identity;
- partitions observations by the governed UES project adapters;
- emits only hashed session/title/source identities and aggregate classifications;
- never emits raw Jules session IDs, titles, or secret material;
- never sends a provider message or creates a Jules session/task.

The persistence layer is intentionally separated from the secret-bearing read path so provider credentials do not need a write-capable workflow job. Any later durable-ingestion integration must preserve that trust split.
