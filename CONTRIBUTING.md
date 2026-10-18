# Contributing

Thanks for wanting to help. Bug reports, fixes and small focused features are
all welcome. For anything larger, open an issue first so the shape can be
agreed before you write it.

For a security problem, do not open an issue: see [SECURITY.md](SECURITY.md).

## Development setup

The full guide is on the docs site:
[Development](https://sebastianbuechler.github.io/container-doctor/contributing/development/),
[Adding a setting](https://sebastianbuechler.github.io/container-doctor/contributing/adding-a-setting/),
[Documentation](https://sebastianbuechler.github.io/container-doctor/contributing/documentation/) and
[Releasing](https://sebastianbuechler.github.io/container-doctor/contributing/releasing/). In short, on Python 3.14 (the
version the `Dockerfile` uses):

```bash
git clone https://github.com/sebastianbuechler/container-doctor.git
cd container-doctor
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests
```

`requirements.txt` is what the image installs; `requirements-dev.txt` adds
what only the tests need (PyYAML, for reading the compose file). No Docker
daemon is needed; the socket proxy tests skip unless
`DOCTOR_PROXY_INTEGRATION=1` is set. CI runs the full suite on every pull
request and push to `main`, and the image and release workflows run the tests
and the proxy suite before they build anything.

A new setting is declared once with `setting()` in `app/config.py`; anything
that decides where a secret is sent, or how sign-in works, must be
`env_only`. Run `python scripts/gen_docs.py` after changing a setting's
description, and build the docs (`cd docs && npm ci && npm run build`) after
changing a page.

## Pull requests

- One change per pull request, with a description of what and why.
- Tests for new behaviour, and for the bug a fix fixes.
- Docs updated: the pages under `docs/src/content/docs` the change touches,
  `.env.example` and `compose.example.yaml` for any new or changed setting,
  and a line under *Unreleased* in [CHANGELOG.md](CHANGELOG.md) for anything a
  user would notice.
- No secrets, real hostnames or personal data in code, tests, fixtures or
  screenshots. Log fixtures should use made-up values.
- Keep the guardrails intact: the model's answer stays a closed set of
  actions, and plain code decides whether one runs. A change that widens what
  the socket proxy grants, or what the model can cause, needs a good reason
  written down.

## Releases

Releases are cut by the maintainer with the **Release** workflow; see
[Releasing](https://sebastianbuechler.github.io/container-doctor/contributing/releasing/).
