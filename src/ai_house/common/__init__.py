"""Code shared line for line with the private deployment this kit was ported from.

The files here are not written for the kit: they are the deployment's own modules, with names and numbers replaced
and a short list of edits (UPSTREAM.md). Change behaviour here only together with that side, and keep the list of
edits current: `tools/drift_report.py` measures how far the two have moved apart.

`loader.load()` gives a caller its own copy of the memory module, bound to its own roster.
"""
