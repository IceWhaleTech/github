# IceWhaleTech reusable workflows

This repository contains shared GitHub Actions workflows used by IceWhaleTech
service repositories.

## OpenAPI SDK Builder

The OpenAPI SDK workflows run the versioned Builder image
`ghcr.io/icewhaletech/openapi-sdk-builder:0.1.0`. Service repositories own
their OpenAPI documents, `openapi-sdk.yaml`, package version, and release
trigger. The shared workflows own generation, compilation, packaging, and npm
publishing behavior.

### Check an SDK

The caller repository must have Read access under the Builder package's
**Manage Actions access** settings.

```yaml
name: Check OpenAPI SDK

on:
  pull_request:
    paths:
      - "api/**/*.yaml"
      - openapi-sdk.yaml
  push:
    branches:
      - main
    paths:
      - "api/**/*.yaml"
      - openapi-sdk.yaml

jobs:
  check:
    permissions:
      contents: read
      packages: read
    uses: IceWhaleTech/github/.github/workflows/openapi_sdk_check.yml@openapi-sdk-v1.0.1
    with:
      config-path: openapi-sdk.yaml
```

### Publish an SDK

The npm package's trusted publisher must name the caller repository and caller
workflow file. npm validates the caller (for example, `npm.yaml`), not
`openapi_sdk_release.yml`. Both the caller and reusable workflow must grant
`id-token: write`.

```yaml
name: Publish npm

on:
  push:
    tags:
      - "v*.*.*"
  workflow_dispatch:
    inputs:
      version:
        description: Semver package version
        required: true
        type: string

jobs:
  publish:
    permissions:
      contents: read
      packages: read
      id-token: write
    uses: IceWhaleTech/github/.github/workflows/openapi_sdk_release.yml@openapi-sdk-v1.0.1
    with:
      config-path: openapi-sdk.yaml
      version: ${{ inputs.version || github.ref_name }}
```

`version` may start with `v`; the Builder removes it before creating the npm
package. `npm-tag` defaults to `latest`.

Repositories whose OpenAPI documents live in Git submodules can pass
`checkout-submodules: true`. If those submodules are private, also pass the
existing `API_TOKEN_GITHUB` secret:

```yaml
    with:
      checkout-submodules: true
    secrets:
      API_TOKEN_GITHUB: ${{ secrets.API_TOKEN_GITHUB }}
```

Callers should pin these workflows to a release tag such as
`@openapi-sdk-v1.0.1` (or a full commit SHA), rather than `@main`.
