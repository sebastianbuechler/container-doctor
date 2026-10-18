## What and why

<!-- What this changes, and the problem it solves. Link an issue if there is one. -->

## Testing

<!-- How you checked it. At least: -->

- [ ] `python -m unittest discover -s tests` passes

## Docs

- [ ] I checked the documentation pages this change touches and updated what is no longer true
- [ ] Settings or `.env.example` changed: I ran `python scripts/gen_docs.py`
- [ ] Pages changed: `cd docs && npm run build` passes
- [ ] `CHANGELOG.md` (*Unreleased*) updated for anything a user would notice
- [ ] Dashboard or API answers changed: I ran `python -m demo.record` and looked at the screenshots

Pages checked or changed:

## Checklist

- [ ] New or changed settings are in `app/config.py`, `compose.example.yaml`
      (`- DOCTOR_X=${DOCTOR_X:-}`) and `.env.example`
- [ ] No secrets, real hostnames or personal data in code, tests, fixtures or screenshots
