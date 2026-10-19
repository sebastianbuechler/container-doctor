# Container Doctor

AI triage for self-hosted Docker containers. Python (FastAPI, docker-py,
SQLite) in `app/`, a no-build dashboard in `app/web/static/`, tests in `tests/`,
the documentation site (Astro Starlight) in `docs/`, and in `demo/` a made-up
host the real doctor runs against for the docs' screenshots and live demo.

## Commands

```bash
pip install -r requirements.txt -r requirements-dev.txt   # dev adds what the tests need
python -m unittest discover -s tests          # all tests, no Docker needed
python scripts/gen_docs.py                    # regenerate the env var reference
python -m demo.record                         # retake screenshots, re-record the demo
cd docs && npm ci && npm run build            # build the docs; fails on broken links
```

## Every change checks the docs

A change is not done until the documentation says what the code now does. After
every change - feature, fix, setting, label, behaviour, UI text:

1. Find the pages it touches: `grep -ri "<feature or setting>" docs/src/content/docs`.
   Update what is now wrong; add what is new to the page it belongs on (see the
   sidebar in `docs/astro.config.mjs`), or a new page listed in that sidebar.
2. A setting changed or added: describe it in `.env.example` or in a comment
   above its `setting()` in `app/config.py`, then run `python scripts/gen_docs.py`.
   Never edit `docs/src/content/docs/reference/environment-variables.md` by hand.
3. A `doctor.*` label changed or added: update `docs/src/content/docs/reference/labels.md`.
4. Pages changed: build the docs (`cd docs && npm run build`).
5. The dashboard looks different, or an API answer changed shape: run
   `python -m demo.record` to retake the screenshots and re-record the live
   demo, and look at the screenshots it wrote. A new view or panel gets a
   `<Screenshot>` on the page that describes it.

`tests/test_docs.py` fails when the generated reference is stale, a label is
undocumented, or a page is missing from the sidebar - but it cannot tell whether
the prose is still true. Reading the affected pages is the part that matters.
Say in the PR which pages you checked or changed.

## Conventions

- Settings are declared once with `setting()` in `app/config.py`; see
  `docs/src/content/docs/contributing/adding-a-setting.md`.
- Docs links are absolute, with the base: `/container-doctor/guides/settings/`.
- Releases are cut by the Release workflow, never by pushing a tag by hand; see
  `docs/src/content/docs/contributing/releasing.md`.
