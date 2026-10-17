---
title: "Releasing"
description: "How a release is cut, and what it publishes."
---

Every push to `main` publishes the image as `edge` and `sha-<commit>`. A
release is cut by the **Release** workflow:

1. Check that `main` is green and the docs are current
   ([Documentation](/container-doctor/contributing/documentation/)).
2. **Actions → Release → Run workflow**, with the version `X.Y.Z` -
   [semantic versioning](https://semver.org): a breaking change to settings,
   labels or the compose file is a major version.

The workflow tags `main` with `vX.Y.Z`, builds and publishes the image as
`X.Y.Z`, `X.Y` and `latest` for amd64 and arm64, and then publishes the GitHub
release with generated notes and the image's digest. The release appears only
once its image can be pulled; a failed run can simply be re-run.
