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
    uses: IceWhaleTech/github/.github/workflows/openapi_sdk_check.yml@openapi-sdk-v1.0.2
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
    uses: IceWhaleTech/github/.github/workflows/openapi_sdk_release.yml@openapi-sdk-v1.0.2
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
`@openapi-sdk-v1.0.2` (or a full commit SHA), rather than `@main`.

## Publish RAW application packages to e2e

Use the immutable `raw-publish-v1.0.0` tag (or full commit SHA). The publisher
imports and verifies a package; device installation is a separate platform action.

Create a repository-scoped credential in **Application artifacts → CI publishing**.
Set Actions Variable `E2E_PLATFORM_URL` and Secret `E2E_RAW_PUBLISH_TOKEN` in the
calling repository, or authorize it to use organization values. The caller owns
network reachability. The runner needs Python 3; local builds also use Git to
record the actual checkout commit.

Add this step at the end of the job that builds the RAW file:

```yaml
- name: Publish RAW to e2e
  id: e2e
  uses: IceWhaleTech/github/actions/publish-raw@raw-publish-v1.0.0
  with:
    platform-url: ${{ vars.E2E_PLATFORM_URL }}
    publish-token: ${{ secrets.E2E_RAW_PUBLISH_TOKEN }}
    package-path: dist/folio.raw
    # source-commit: actual 40-character build commit (defaults to checkout HEAD)
    # source-ref: refs/tags/v1.2.3 (defaults to github.ref)
    # expected-version: 1.2.3 (optional exact package version assertion)
```

No Actions artifact upload is needed. Alternatively, publish an existing HTTP(S)
download URL with the URL-only reusable workflow:

```yaml
jobs:
  publish-e2e:
    needs: build
    permissions:
      contents: read
    uses: IceWhaleTech/github/.github/workflows/raw_artifact_publish.yml@raw-publish-v1.0.0
    with:
      package-url: ${{ needs.build.outputs.raw-url }}
      sha256: ${{ needs.build.outputs.raw-sha256 }}
      source-commit: ${{ needs.build.outputs.source-commit }}
      source-ref: ${{ needs.build.outputs.source-ref }}
      filename: folio.raw
      # platform-url defaults to caller vars.E2E_PLATFORM_URL
      # runs-on: '["self-hosted", "Linux", "X64", "my-network"]'
    secrets:
      E2E_RAW_PUBLISH_TOKEN: ${{ secrets.E2E_RAW_PUBLISH_TOKEN }}
```

`build` is caller-defined and must output the actual source commit/ref and package
checksum. The platform must be able to download the URL; private GitHub web URLs
are not direct download links. Signed URLs must last through ingestion. An import
source does not become a managed replica and is not deleted when the platform
artifact is deleted.

The Action accepts exactly one of `package-path` / `package-url`. URL mode requires
`sha256`; local files are streamed and hashed automatically. `filename` preserves
the original download name. The package manifest is authoritative for its version;
git tags are recorded separately. The Action returns `task-id`, `sha256`, verified
`artifact-id` and `artifact-url`, and writes a step summary. The reusable workflow
returns task and artifact outputs. Import failures and timeouts fail the job;
timeouts leave the platform task running and preserve its ID. Default wait is
1800 seconds (`wait-timeout` input).

Same repository/run/attempt/SHA/filename reuses the original task. Changed build
metadata under that identity is rejected. Distinct attempts retain provenance
while deduplicating bytes. Failed imports require a new GitHub run attempt, rather
than silently creating another task. Credentials cannot delete packages, access
settings, or deploy to devices. Rotate the Secret before revoking the old token.

Use credentials only in trusted branches/tags or protected manual jobs. Do not
expose them to untrusted PRs/forks or execute their code using `pull_request_target`.
Local mock-HTTP tests run without credentials or external services. The package's
Action and reusable workflow share the same immutable `raw-publish-v*` release tag;
merge and pass checks before tagging, and never move existing tags. Real service
integration requires caller configuration after isolated acceptance.
