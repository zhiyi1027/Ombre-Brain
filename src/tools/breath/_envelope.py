"""Shared markers for the human-readable Breath envelope.

Stored bucket bodies are rendered verbatim, so the trace parser must not infer
synthetic sections from a heading that could also appear inside remembered
text. Startup inserts this marker immediately before the generated daily
impression; the marker is not persisted in the impression itself.
"""


DAILY_IMPRESSION_SENTINEL = "<!-- ombre:section=daily_impression -->"
