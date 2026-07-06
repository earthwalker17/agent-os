# Codebase Recon Method
> An efficient reading order for unfamiliar code.

1. Read the README and top-level config/manifest first — learn the language, framework, entry points, and scripts before opening any source file.
2. List the directory tree one or two levels deep; name the 3–5 zones (app code, tests, config, assets) instead of opening files at random.
3. Find the entry point (main, server bootstrap, CLI) and trace startup: what gets initialized, in what order.
4. Follow one representative request or command end to end — route, handler, data layer, response — and note each hop.
5. Read the data model (schemas, migrations, core types) — it explains more per line than any handler.
6. Skim tests for the area in question; they document intended behavior and edge cases.
7. Search for the exact identifier or string tied to the current question; read only the files that match.
8. Stop when you can answer the question — state what you read and what you skipped.

Caveats: never claim behavior of a file you did not open; say "unread" instead. Prefer breadth-first skims, then one deep dive.
