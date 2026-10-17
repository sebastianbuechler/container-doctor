---
title: "Adding a setting"
description: "Declare a setting once and have it everywhere."
---

Declare it once, as a field of `Settings` in `app/config.py`:

```python
poll_interval_seconds: int = setting("DOCTOR_POLL_INTERVAL", 30)
mode: Mode = setting("DOCTOR_MODE", "observe", choices=Mode, convert=_validate_mode)
docker_host: str = setting("DOCKER_HOST", "tcp://doctor-socket-proxy:2375", restart=True)
```

`setting()` records the variable, default, type (taken from the default),
choices, and whether it is a `secret`, `env_only`, `can_be_off` or needs a
`restart`; validation, the Settings page, export and import all read it from
there. A setting with a closed set of values takes a `StrEnum` as `choices`,
and a value added to the enum appears on the page with nothing else to change.
Then:

- pass it in `compose.example.yaml` as `- DOCTOR_X=${DOCTOR_X:-}` and document
  it in `.env.example`;
- show it in `Settings.overview()` and map its row to the variable in `ENV_FOR`;
- describe it in a comment - in `.env.example`, or above its declaration - and
  run `python scripts/gen_docs.py` to put it in the
  [environment variable reference](/container-doctor/reference/environment-variables/);
- if it is read where the doctor is built - a constructor, `start()`,
  `build_app()` - mark it `restart=True`, or rebuild what uses it in
  `Supervisor.apply_settings`.

`tests/test_settings_env.py` checks `compose.example.yaml`, `.env.example`
and the page agree, and `tests/test_live_settings.py` fails if a setting read where
the doctor is built is neither marked `restart` nor rebuilt.
