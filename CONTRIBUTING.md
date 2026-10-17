# Contributing

Thanks for wanting to help. Bug reports, fixes and small focused features are
all welcome. For anything larger, open an issue first so the shape can be
agreed before you write it.

For a security problem, do not open an issue: see [SECURITY.md](SECURITY.md).

## Development setup

The image runs Python 3.14 (the version is read from the `Dockerfile`), and
the tests should run on the same.

```bash
git clone https://github.com/sebastianbuechler/container-doctor.git
cd container-doctor
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

`requirements.txt` is what the image installs; `requirements-dev.txt` adds
what only the tests need (PyYAML, for reading the compose file).

## Running the tests

```bash
python -m unittest discover -s tests
```

No Docker daemon is needed. A few tests start the real socket proxy image to
check what it permits, and skip by default; with Docker running:

```bash
DOCTOR_PROXY_INTEGRATION=1 python -m unittest tests.test_socket_proxy -v
```

CI runs the full suite on every pull request and push to `main`, and the
image and release workflows run the tests before they build anything.

To try a change against a real host, build the image and point the compose
file at it, or run `python -m app.selftest` and `python -m app.preflight`
inside a running container to check the model and the Docker endpoint.

## Adding a setting

A setting is declared once, as a field of `Settings` in `app/config.py`:

```python
poll_interval_seconds: int = setting("DOCTOR_POLL_INTERVAL", 30)
```

`setting()` records the variable, its default and type, its choices, and
whether it is a `secret`, `env_only` (never saved from the Settings page),
`can_be_off`, or needs a `restart`. Anything that decides where a secret is
sent, or how sign-in works, must be `env_only`. Then:

- pass it in `compose.example.yaml` as `- DOCTOR_X=${DOCTOR_X:-}` - with no
  default there, so the code's default applies;
- document it in `.env.example`, commented out, with its default and a line
  on what it does;
- show it in `Settings.overview()` and map its row to the variable in
  `ENV_FOR`;
- if it is read where the doctor is built, mark it `restart=True` or rebuild
  what uses it in `Supervisor.apply_settings`;
- mention it in [`docs/guide.md`](docs/guide.md) where the feature is
  described.

`tests/test_settings_env.py` fails if `compose.example.yaml`, `.env.example`
and the Settings page disagree, and `tests/test_live_settings.py` if a
setting read at build time is neither marked `restart` nor rebuilt.

## Pull requests

- One change per pull request, with a description of what and why.
- Tests for new behaviour, and for the bug a fix fixes.
- Docs updated: the guide, `.env.example` and `compose.example.yaml` for any
  new or changed setting, and a line under *Unreleased* in
  [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- No secrets, real hostnames or personal data in code, tests, fixtures or
  screenshots. Log fixtures should use made-up values.
- Keep the guardrails intact: the model's answer stays a closed set of
  actions, and plain code decides whether one runs. A change that widens what
  the socket proxy grants, or what the model can cause, needs a good reason
  written down.

## Releases

Releases are cut by the maintainer with the **Release** workflow (Actions →
Release → Run workflow, with a version `X.Y.Z`). From `main`, it runs the
tests, tags `vX.Y.Z`, builds and publishes the image as `X.Y.Z`, `X.Y`,
`latest` and `sha-<commit>`, and then publishes the GitHub release with
generated notes - in that order, so a release is never announced without its
image. Every push to `main` publishes `edge` and `sha-<commit>`.
