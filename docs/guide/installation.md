---
description: Install Privyx with pip, uv, or Docker, pick the optional extras you need, and check that the installation works.
---

# Installation

Privyx is a Python package with a command-line program, `privyx`. It needs
**Python 3.12 or newer**. A Docker image is published for every release.

## Install

=== "pip"

    ```bash
    pip install privyx
    ```

    On an older Python, pip stops with `No matching distribution found for
    privyx`. Check with `python --version`.

=== "uv"

    ```bash
    uv tool install privyx       # the `privyx` command, in its own environment
    ```

    Or add it to a project: `uv add privyx`.

=== "No install"

    ```bash
    uvx privyx --version
    ```

    [`uvx`](https://docs.astral.sh/uv/guides/tools/) downloads Privyx into a
    cache and runs it. Every `privyx ...` command in this documentation works
    as `uvx privyx ...`.

=== "Docker"

    ```bash
    docker run --rm ohp1x/privyx --version
    ```

    The image holds every extra below. See [Docker](../docker.md).

This installs the proxy, the privacy engine, and the CLI.

## Extras

Extras switch on optional components. Nothing below is needed for the default
setup.

| Extra | Installs | Needed for |
|---|---|---|
| `sqlite` | aiosqlite | [`vault.type: sqlite`](sessions.md#vault-backends) |
| `redis` | redis | [`vault.type: redis`](sessions.md#vault-backends) |
| `presidio` | Presidio, spaCy | the [`presidio` detector](detection.md#presidio) |
| `providers` | OpenAI and Anthropic SDKs | the [`llm` detector](detection.md#llm-detector) |
| `faker` | Faker | the [`faker` operator](masking.md#faker) |
| `crypto` | cryptography | the [`encrypt` operator](masking.md#encrypt) |

Combine them as needed:

=== "pip"

    ```bash
    pip install 'privyx[sqlite,faker]'
    ```

=== "uv"

    ```bash
    uv tool install 'privyx[sqlite,faker]'
    ```

=== "No install"

    ```bash
    uvx --from 'privyx[sqlite,faker]' privyx --version
    ```

A setting that needs a missing extra fails at startup, with the install
command in the message:

```console
$ privyx proxy -c sqlite.yaml
Error: aiosqlite is required. Install with `pip install privyx[sqlite]`.
```

## Check the installation

```console
$ privyx --version
privyx, version 0.1.14
$ privyx doctor
Privyx Doctor
=============
  ✓ plugins: no plugin paths configured
  ✓ detector: regex: 1 span(s) ['EMAIL']
  ✓ vault: memory: round-trip ok
  ✓ provider: generic → http://localhost:20128
  ✓ proxy: request transformed, 1 mapping(s) vaulted
  ✓ streaming: pseudonym restored across a split chunk
  ✓ configuration: valid: detector=regex policy=default operator=pseudonym vault=memory anchor=off
```

`privyx doctor` builds every configured component and pushes a sample request
through it, without calling a provider. Run it with `-c your.yaml` after
changing a config file.

## Upgrade

=== "pip"

    ```bash
    pip install --upgrade privyx
    ```

=== "uv"

    ```bash
    uv tool upgrade privyx
    ```

=== "No install"

    ```bash
    uvx privyx@latest --version
    ```

=== "Docker"

    ```bash
    docker pull ohp1x/privyx
    ```

The [changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md) lists
what each release changed. Privyx is in its `0.1.x` series: a release can
change a default, and the changelog says so under "Changed".

## Next steps

- [Quickstart](getting-started.md): mask a first request.
- [How it works](how-it-works.md): what happens to a request, and the terms
  used in these pages.
