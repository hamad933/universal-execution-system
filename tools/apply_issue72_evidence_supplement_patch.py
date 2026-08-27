from pathlib import Path

path = Path("ues/initial_lineage_runtime.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        "from .generation_transition import initial_lineage_transition_key\n",
        "from .generation_transition import initial_lineage_transition_key\n"
        "from .evidence_supplement_runtime import evidence_supplement_entries, run_evidence_supplements\n",
    ),
    (
        "    entries = _authority_entries(authority)\n    if not entries:\n",
        "    entries = _authority_entries(authority)\n"
        "    supplement_entries = evidence_supplement_entries(authority)\n"
        "    if not entries and not supplement_entries:\n",
    ),
    (
        "    source_name, source_proven = _source_for_repository(jules, repository)\n",
        "    source_name, source_proven = _source_for_repository(jules, repository) if entries else (None, False)\n",
    ),
    (
        "\n    decisions = Counter(\n",
        "\n    if supplement_entries:\n"
        "        results.extend(\n"
        "            run_evidence_supplements(\n"
        "                adapter=adapter,\n"
        "                authority=authority,\n"
        "                entries=supplement_entries,\n"
        "                store=store,\n"
        "                jules=jules,\n"
        "                github=github,\n"
        "                inventory=inventory,\n"
        "                provider_observation=provider_observation,\n"
        "                actor=actor,\n"
        "            )\n"
        "        )\n\n"
        "    decisions = Counter(\n",
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one integration anchor, found {count}: {old!r}")
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
