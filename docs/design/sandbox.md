# Design: Sandbox / Reproducible Environment

**Status: planned · 2026-05-03**

A way for an external agent (or a fresh dev machine) to spin up a
runnable localaiproject environment with one command.

## Why

Today setup is:
1. Install Python 3.13
2. `pip install -r requirements.txt` (PySide6 ~80 MB)
3. Install Ollama and pull models (separate process, 8 GB+ per model)
4. Maybe `pip install duckdb` if opting into the DuckDB backend
5. Maybe `pip install python-lsp-server` if you want LSP diagnostics

That's friction for an agent that wants to play with the project — and
fragile, because Python version drift, OS differences, and missing
system libraries (libgl on Linux for Qt) bite often.

Two options for sandboxing, neither replaces the other:

### Option A — Dockerfile

Easy: a `Dockerfile` builds an image with Python 3.13, the requirements
preinstalled, Ollama bundled, and the test suite passing on first run.
Works on any host with Docker. UI is awkward in containers but the CLI
+ gateway work fine.

### Option B — Nix flake

Reproducible: `flake.nix` defines a dev shell with all deps pinned by
hash. Anyone with Nix gets the exact same environment.  No image
download, but no Windows support unless WSL2.

Ship Dockerfile first (broader audience). Add the flake when someone
asks.

## Dockerfile design

### Image base

`python:3.13-slim-bookworm`. Bookworm has libgl + libxkbcommon
required by PySide6. Slim keeps it under 200 MB before deps.

### Layout

```dockerfile
FROM python:3.13-slim-bookworm

# System deps for Qt offscreen rendering + git for any inline cloning
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libegl1 libxkbcommon0 libdbus-1-3 \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Ollama (optional — comment out if you want to bring-your-own)
RUN curl -fsSL https://ollama.com/install.sh | sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir duckdb python-lsp-server  # optional extras

COPY . .

ENV QT_QPA_PLATFORM=offscreen
ENV MEMORY_BACKEND=duckdb
ENV PYTHONUNBUFFERED=1

# Default: launch the gateway and idle
CMD ["python", "-m", "app.main"]
```

### Build / run

```bash
$ docker build -t localaiproject .
$ docker run --rm -it -p 7780:7780 localaiproject

# Or one-shot via the (planned) CLI
$ docker run --rm localaiproject python -m app.cli -c doctor --json
```

### Volumes for persistence

Mount `/app/app/memory/` and `/app/app/ui/themes_qss/` if you want
state and themes to survive container rebuilds.

```bash
$ docker run --rm -it -p 7780:7780 \
    -v $(pwd)/data:/app/app/memory \
    -v $(pwd)/themes:/app/app/ui/themes_qss \
    localaiproject
```

### What it can't do (without more work)

- **Pull Ollama models in the build** — they're 7+ GB and shouldn't
  bloat the image. Use `docker exec` after first run, or mount Ollama's
  model dir as a volume from the host.
- **Show the Qt UI** — needs X11 or Wayland forwarding; out of scope.
  Use the host UI; use the container for the headless API + tests.
- **Watch for code changes** — if you want hot reload, use `-v
  $(pwd):/app` to bind-mount the source.

## flake.nix design

```nix
{
  description = "localaiproject dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";  # or x86_64-darwin / aarch64-darwin
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python313
          python313Packages.pip
          python313Packages.requests
          python313Packages.psutil
          python313Packages.duckdb
          # Qt deps for offscreen rendering
          libGL
          libxkbcommon
          dbus
          # Tooling
          ollama
          git
        ];

        shellHook = ''
          python -m venv .venv
          source .venv/bin/activate
          pip install -r requirements.txt --quiet
          export QT_QPA_PLATFORM=offscreen
          echo "localaiproject dev env ready. Run: python -m app.main"
        '';
      };
    };
}
```

`nix develop` drops into a shell with everything pinned. The `.venv`
is for Python wheels Nix doesn't have first-class packages for.

## CI integration

Once the Dockerfile lands, GitHub Actions can run the test suite in
under a minute:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t localaiproject .
      - run: |
          docker run --rm localaiproject \
            sh -c "QT_QPA_PLATFORM=offscreen python -m unittest discover tests"
```

## Phasing

### Phase 1 — Dockerfile (this round)
- The `Dockerfile` above
- A `.dockerignore` that excludes `.git/`, `_design/`, `*.duckdb`
- README section explaining build + run + volumes
- Smoke-test by running the test suite inside the image

### Phase 2 — Nix flake
- Add `flake.nix` and `flake.lock`
- Dev-shell-only (no derivation building the app yet)
- Note in README; document for Nix users

### Phase 3 — Docker Compose
- One-command `docker compose up` that starts localaiproject + Ollama
  in linked containers
- Pre-seeds the required models on first start (background pull)
- Optional second service for an external agent (e.g. a Claude Code
  container) to demonstrate cross-process flow

### Phase 4 — per-iteration git worktree (sandcastle-inspired)

A complementary isolation primitive at the *git* layer rather than
the container layer. When a long-running task (cowork run, an
iteration loop, an external-agent run) wants its own scratch
filesystem on the same repo:

```
> session arch
> workspace ~/proj
> workspace worktree on-branch feat/big-refactor
… session.context.workspace_path now points at a fresh git worktree
… commits land on feat/big-refactor; HEAD is unchanged
> workspace worktree merge        # fast-forward main
> workspace worktree discard      # throw away the branch + worktree
```

Why bother on top of the container sandbox:
- Multiple cowork runs can target the same repo without stepping on
  each other (each gets its own worktree dir)
- Cheaper than a container per run (worktrees are filesystem-only)
- Naturally maps to `cowork-mode.md` Phase 5's branch-strategy dial
- Doesn't replace the container — they compose: container + per-run
  worktree gives the strongest isolation

Implementation note: Claude Code already creates a worktree per agent
session under `.claude/worktrees/`. Phase 4 just exposes the same
capability to localaiproject sessions. Depends on
`workspace-writes.md` Phase 1.5 (the git-aware write hook) so writes
inside a worktree commit cleanly.

## What this enables

- An agent can `docker run … python -m app.cli -c doctor --json` to
  inspect a fresh localaiproject without touching the host
- CI runs the full test suite in a known-clean environment
- Nix users get a reproducible dev shell
- Someone evaluating the project can try it without installing
  anything they wouldn't normally have

## Hard rules

- **Loopback gateway only.** The Dockerfile binds 127.0.0.1; if you
  want network access publish the port explicitly with `-p 7780:7780`
  and **set `LOCALAI_GATEWAY_TOKEN`** so the bearer-auth gate is on.
- **Don't bake secrets.** The image has no API keys, no DuckDB data,
  no Ollama models. All ephemeral / mounted.
- **Don't build a "production" image.** This is for development +
  agents-trying-it-out. A real production deploy needs different
  trade-offs (rootless, distroless, healthchecks, etc.) that we don't
  ship today.

## Open questions

- Multi-arch build? `docker buildx` supports arm64 + amd64; one tag
  for both. Probably yes; ~2× build time, much wider reach.
- Pin Ollama version in the install script call? The shell installer
  always grabs latest. We might want `OLLAMA_VERSION=0.12.7 sh` in
  the Dockerfile to avoid drift.
- A `tests` image vs an `app` image? Single image keeps it simple;
  multi-stage if size becomes an issue.
