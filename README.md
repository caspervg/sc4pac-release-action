# sc4pac-release-action

A GitHub Action that keeps a DLL's [sc4pac](https://memo33.github.io/sc4pac/) metadata in step with its GitHub releases. It runs after your release job and edits the package's YAML file in an sc4pac channel. It then pushes the change to your fork of that channel, and you open the pull request with one click.

It was written for the SimCity 4 DLLs in [sebamarynissen/simtropolis-channel](https://github.com/sebamarynissen/simtropolis-channel), but it works for any channel that uses the standard sc4pac layout.

## What it changes

For the asset whose `assetId` you give it, it:

- sets the asset's `version`, `lastModified` and `url` to the new release,
- sets the `version` of every package that installs files from that asset,
- recomputes the `sha256` of every file those packages install through `withChecksum` (DLLs, INIs), taken from the archive that sc4pac users will download,
- follows a version number in an `include` pattern (`/Setup-0.8.2.exe`) or in `info.warning`.

Values are replaced in place, so quoting, comments and formatting stay as the maintainers wrote them, and the diff shows only the values that changed. The result is checked with the channel's own linter ([memo33/sc4pac-actions](https://github.com/memo33/sc4pac-actions)) before anything is pushed.

It also:

- skips drafts and prereleases,
- verifies the downloaded archive against the digest GitHub reports for it,
- never moves a channel back to an older version,
- does nothing when the channel already describes the release, and then leaves `lastModified` alone too,
- adds a `-1` release suffix when the same version is released again with different files,
- warns about a DLL, ASI, EXE or INI in the archive that no package installs.

## Pull requests and tokens

Opening a pull request in a repository you are not a member of takes a classic personal access token, which can write to every public repository of your account. This action doesn't need one:

1. It pushes to a branch named `sc4pac-release/<assetId>` in your fork. A fine-grained token limited to that fork is enough.
2. It links to a prefilled pull request form in the job summary and as an annotation. You click it and submit.

There is one branch per asset. A newer release replaces the commit on it, so an unmerged pull request moves on to the newest version by itself, and old pull requests never pile up. The title and description name no version, because a fork-only token can't edit them. The commit message lists the version, URL and checksums.

When some other open pull request touches the same YAML file, for example a maintainer's manual update, the job warns about it.

Pass `open-pull-request: true` together with a classic token if you do want the action to open the pull request itself.

### Setup

1. Fork the channel, e.g. [sebamarynissen/simtropolis-channel](https://github.com/sebamarynissen/simtropolis-channel/fork).
2. Create a [fine-grained token](https://github.com/settings/personal-access-tokens/new):
   - Repository access: *Only select repositories*, and pick only your fork.
   - Permissions: *Contents: Read and write*. Nothing else.
3. In the DLL's repository, create an environment (Settings → Environments), for example `sc4pac`. Under *Deployment branches and tags*, allow only your release tags, e.g. `v*`. Add the token there as the secret `SC4PAC_FORK_TOKEN`. Only jobs that run for a release tag can then read it.

Without a token, the action still finds the release, updates and lints the metadata, and shows the diff in the job summary, but pushes nothing.

## Usage

Add a job after the one that publishes the release:

```yaml
  sc4pac:
    needs: release
    runs-on: ubuntu-latest
    environment: sc4pac
    concurrency: sc4pac-caspervg-scd3d11
    steps:
    - uses: caspervg/sc4pac-release-action@v1
      with:
        channel: sebamarynissen/simtropolis-channel
        asset-id: caspervg-scd3d11
        token: ${{ secrets.SC4PAC_FORK_TOKEN }}
```

Use a `needs:` job rather than a separate `on: release` workflow: a release created with the workflow's own `GITHUB_TOKEN` doesn't trigger other workflows.

### Inputs

| Input | Default | |
|---|---|---|
| `channel` | | Channel repository, e.g. `sebamarynissen/simtropolis-channel`. |
| `asset-id` | | The `assetId` of the release archive in that channel. |
| `asset-name` | the release's only `.zip` | File name of the release asset, when the release has more than one `.zip`. |
| `tag` | the triggering tag | Release tag. |
| `repository` | this repository | Repository that holds the release. |
| `version` | the tag without its `v` | Version for sc4pac. |
| `src-path` | `src/yaml` | Folder of the channel's YAML files. |
| `base-branch` | `main` | Branch of the channel to target. |
| `fork` | `<owner>/<channel name>` | Your fork of the channel. |
| `token` | | Token that can push to the fork. |
| `open-pull-request` | `false` | Open the pull request instead of linking to the form; needs a classic token. |

### Outputs

| Output | |
|---|---|
| `changed` | `true` when the metadata needed a change for this release. |
| `pull-request` | URL of the pull request, or of the prefilled form that opens it. |

## Running the updater by hand

`update_metadata.py` only needs Python 3 and PyYAML:

```sh
python update_metadata.py --channel-dir simtropolis-channel --asset-id caspervg-scd3d11 \
  --version v0.4.4 --url https://github.com/caspervg/scd3d11/releases/download/v0.4.4/SCD3D11-v0.4.4.zip \
  --last-modified 2026-10-30T18:18:43Z --archive SCD3D11-v0.4.4.zip --summary summary.md
```

## Packages in memo33/sc4pac

The main channel updates its packages from Simtropolis on its own: when a new version is uploaded to the STEX, its update check rewrites the GitHub URL and the checksums. For a DLL that is listed there with a STEX `nonPersistentUrl`, upload to the STEX and let that check do the work. Type the version without a leading `v`, because it is pasted into the download URL. Running this action against the main channel as well would race that update check.
