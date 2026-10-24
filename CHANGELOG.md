# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-10-24

### Added

- A network note that is meant can be dismissed. It moves under *dismissed*,
  saying who and when, stops counting towards the page's warnings, and stays
  dismissed from poll to poll until restored.

### Changed

- A `macvlan` or `ipvlan` network is explained on its card on the Network
  page - its parent interface, and that the host cannot reach its containers
  without a shim - rather than as a note in the list above.

## [0.4.1] - 2026-10-24

### Changed

- *Free space not watched* under Disk arrays gives, on a Synology, the lines
  to add: the `mkdir`, the mounts and the whole `DOCTOR_DISK_PATHS`.

### Fixed

- The docs said Docker creates a missing folder for a watched volume. On a
  Synology it does not, and the deploy fails; they now say to create it first,
  and the example uses a folder at the volume's root (`@container-doctor-disk`)
  rather than one in a shared folder.
- A Traefik started with `--providers.docker.network` (or
  `TRAEFIK_PROVIDERS_DOCKER_NETWORK`) now counts as `traefik.docker.network`
  for every container without the label: routed containers on several
  networks are no longer called ambiguous, and are warned about when they are
  not on that network.

## [0.4.0] - 2026-10-24

### Added

- A drive's page charts its reads and writes over 24 hours or 7 days, with
  the totals and its busiest moment, from the kernel's own counters.
- A drive's page shows its SMART counters over the last 30 days, one reading
  an hour: which stayed steady, which rose and when, and spin-ups per day.
- Each container's CPU is kept per hour for 14 days, charted on its page with
  its busiest hour, and the Host page names the containers that used the most
  CPU over the chosen range.
- A hard disk's page shows when it slept over the last 24 hours or 7 days: a
  bar of asleep and awake, the share of time asleep and how often it woke.
  Read from the SMART sidecar, which never wakes a drive to ask.
- A hard disk's page shows how often it has spun up (`Start_Stop_Count`).
- Each watched path names the volume it is on, every volume under Disk
  arrays shows how full it is, and volumes no watched path is on are listed
  with how to watch them (docs: *Watching every volume*).

### Changed

- A drive asleep keeps its serial and model on the Storage page, remembered
  from its last reading awake, so its temperature history stays found too.

### Fixed

- A watched path whose mount moved to another volume no longer charts the
  old volume's readings as its own: the chart and the fill forecast start
  again at the move, and a line under the chart says when.

## [0.3.0] - 2026-10-20

### Changed

- The documentation's start page leads with the dashboard: a short pitch, a
  tour of four views (with the phone version, side by side on a phone), and
  how it works in three steps.
- Storage pools are told apart as SHR layers, a single RAID, or an SSD cache
  in front of a volume. A cache is drawn in its own colour as *SSD cache for
  Volume N* rather than as a layer, only layered pools explain SHR, and the
  "Drive N is not in this layer" notes are gone.
- Pools and volumes are named as Storage Manager names them, also when a
  DSM 6 pool (`vg1000`) sits beside a newer one.
- A SATA SSD's wear warns at `DOCTOR_SMART_WEAR_PCT`, as NVMe already did.

### Added

- Each volume shows its filesystem (Btrfs, ext4, XFS), mounted in the doctor
  or not, and a pool shows how much of it is not in a volume yet.
- A drive's page shows more of SMART: SSD or rpm, link speed, data written and
  read, SSD life used, each sector counter (zeros included), CRC errors, head
  parks, logged errors and failed self-tests.

- The live demo runs the classifier in `on`, with two weeks of its verdicts:
  noise it hid, severities it raised and lowered, and signals it left to the
  rules. The classifier guide shows its panel.
- The documentation site counts visits with Cloudflare Web Analytics: no
  cookies, no fingerprinting. Only the published site does; see
  *Contributing → Documentation*.

### Fixed

- The live demo's containers, image updates, internet checks and host tiles no
  longer stay on their loading placeholders, and its poll indicator no longer
  reads "Poll overdue".

## [0.2.0] - 2026-10-18

### Upgrading from 0.1.0

- **Configure sign-in**: without `DOCTOR_WEB_PASSWORD` (12 characters or
  more, not a placeholder) or OIDC, the dashboard and API refuse every request.
- **Check the data directory's owner**: the image runs as `1000:1000`. If
  yours is owned by `1026:100`, set `DOCTOR_UID=1026` and `DOCTOR_GID=100`, or
  `chown -R 1000:1000` it.
- **Move environment-only settings** you saved on the Settings page into the
  stack environment (the list is under *Security* below); saved values for
  them are no longer read.
- **Set `DOCTOR_NTFY_TOPIC`** if you use ntfy; it no longer has a default.
- **Add `--profile smart`** (or `COMPOSE_PROFILES=smart`) to keep the SMART
  sidecar from `compose.example.yaml`.

### Security

- With neither `DOCTOR_WEB_PASSWORD` nor OIDC configured, the dashboard and
  API refuse every request (`403 Set DOCTOR_WEB_PASSWORD or configure OIDC`)
  except `/healthz`, the sign-in status the page reads, and static files.
  Reads used to be open.
- Known placeholder passwords (`change-me`, `changeme`, `change_me`,
  `password`, `admin`, `doctor`, `secret`, `123456`, or the user name) are
  refused with a logged warning, and sign-in is treated as unconfigured. A
  password shorter than 12 characters logs a warning at startup.
- Every state-changing request (`POST`, `PUT`, `DELETE`) must come from the
  dashboard's own origin (`Origin` / `Sec-Fetch-Site` check).
- Repeated wrong passwords are rate-limited: after 10 failures within 5
  minutes a client gets `429` for the rest of that window.
- Settings that decide where a secret is sent, or how sign-in works, are
  environment-only and can no longer be saved from the Settings page:
  `DOCTOR_OPENAI_BASE_URL`, `DOCTOR_CLASSIFIER_URL`, `DOCTOR_NTFY_URL`,
  `DOCKER_HOST` (`DOCTOR_DOCKER_HOST`), every `DOCTOR_OIDC_*`,
  `DOCTOR_PUBLIC_URL`, `DOCTOR_SESSION_HOURS`, `DOCTOR_WEB_USER` and
  `DOCTOR_WEB_PASSWORD_FALLBACK`.
- Responses carry `Content-Security-Policy` (own origin only,
  `frame-ancestors 'none'`), `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.
- Redaction also catches URLs with a password and no user name
  (`redis://:pass@host`).
- Container command lines, entrypoints and health-check commands are
  sanitised before they are stored (sensitive option values kept as a hash),
  and on the first start after upgrading the stored command lines, change
  timeline, incident evidence and LLM request log are run through the new
  redaction. Data captured before - in backups, or already sent to an LLM
  provider - is not affected: rotate credentials if real ones were captured.
- The `doctor-smart` sidecar in `compose.example.yaml` is opt-in, behind the
  Compose profile `smart`, since it has raw disk access and `CAP_SYS_ADMIN`.

### Added

- Screenshots of every dashboard view in the documentation, in light and dark,
  and a [live demo](https://sebastianbuechler.github.io/container-doctor/getting-started/live-demo/):
  the real dashboard replaying a recording of a made-up host. Both are made
  with `python -m demo.record`, which runs the doctor against stand-ins for
  Docker, the host and the model (`demo/`).

### Changed

- **The image runs as UID:GID `1000:1000`** (was `1026:100`). Compose's
  `user:` still overrides it. An existing install whose data directory is
  owned by `1026:100` must set `DOCTOR_UID=1026` and `DOCTOR_GID=100`, or
  `chown -R 1000:1000` the directory.
- The default model is `claude-opus-5-5`.
- Built-in prices for `claude-opus-5-5` ($4 / $20 per million tokens in /
  out), `claude-sonnet-5-5` ($2 / $10), `claude-haiku-5-5` ($0.10 / $0.50) and
  `claude-fable-5` ($10 / $50).
- `DOCTOR_DISK_CRITICAL` defaults to 98% (was 95%, the same as the warning
  threshold).
- `DOCTOR_NTFY_TOPIC` has no default: notifications are off until a topic is
  set. On ntfy.sh, pick a long random one.
- Without `DOCTOR_SESSION_SECRET`, the OIDC session secret is generated once
  and kept in the data directory (`/data/session_secret`, mode `0600`), so a
  restart no longer signs everyone out.
- A manual re-diagnosis counts against the daily budget.
- Responses are gzip-compressed.
- The dashboard shows the release version next to the commit.
- The image and release workflows run the tests before building.

### Fixed

- An approval is claimed atomically, so it can never be executed twice.
- A confidence of NaN or infinity from a model is treated as 0.
- A successful `stop_container` is verified as stopped.

### Removed

- `DOCTOR_NTFY_PRIORITY`, which was never read.

[Unreleased]: https://github.com/sebastianbuechler/container-doctor/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/sebastianbuechler/container-doctor/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/sebastianbuechler/container-doctor/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/sebastianbuechler/container-doctor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/sebastianbuechler/container-doctor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/sebastianbuechler/container-doctor/compare/v0.1.0...v0.2.0
