---
title: "Documentation"
description: "How the docs are built, and the rule that every change checks them."
---

These docs are an [Astro Starlight](https://starlight.astro.build) site in
`docs/`, published to GitHub Pages from `main`.

## Every change checks the docs

A change is not done until the docs say what the code does:

1. **Read the pages your change touches.** Search the site, or
   `grep -ri "<the feature>" docs/src/content/docs`. A new setting, label,
   incident kind, action, page or behaviour belongs somewhere; a changed one
   probably already is somewhere, and is now wrong.
2. **Regenerate the reference.** The environment variable reference is
   generated from `app/config.py` and `.env.example`:

   ```bash
   python scripts/gen_docs.py
   ```

   A setting is described by its comment in `.env.example`, or the comment
   above its declaration in `app/config.py` - write one there rather than
   editing the generated page. The tests fail when the page is out of date,
   and when the code reads a `doctor.*` label the
   [labels page](/container-doctor/reference/labels/) does not list.
3. **Build it** when you changed pages:

   ```bash
   cd docs && npm ci && npm run build
   ```

   The build fails on broken internal links.

## Writing

- One page per task or topic; put it in the sidebar in `docs/astro.config.mjs`.
- Lead with what the reader can do, then why. Short paragraphs, real examples.
- Link instead of repeating: settings to the
  [environment variable reference](/container-doctor/reference/environment-variables/),
  labels to [Container labels](/container-doctor/reference/labels/).
- Asides (`:::note`, `:::caution`) for what a reader must not miss - sparingly.

## Running it locally

```bash
cd docs
npm ci
npm run dev   # http://localhost:4321/container-doctor/
```
