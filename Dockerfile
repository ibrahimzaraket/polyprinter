# Deps baked into the image — NEVER a /tmp venv (see CLAUDE.md).
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY polyprinter ./polyprinter
RUN pip install --no-cache-dir . \
    # `pip install .` copies into site-packages but leaves this source dir
    # in place. Two importable copies of the same package on disk is a
    # real footgun: `python -m x` / `python -c` put cwd (/app) on
    # sys.path[0] and find this copy, but `python path/to/script.py`
    # (file-path invocation) puts the SCRIPT's directory on sys.path[0]
    # instead, skips this copy, and silently resolves to the
    # site-packages one — a different module object with its own
    # __file__-relative paths. Removing the redundant source copy makes
    # every invocation style resolve to the one real install.
    && rm -rf ./polyprinter ./build ./polyprinter.egg-info

COPY config.yaml ./
COPY scripts ./scripts

# data/ is a mounted volume (db + logs live there, gitignored on the host).
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
# Belt-and-suspenders alongside the fix above: config.py/conn.py/obs/log.py
# resolve paths from this instead of a package __file__, so they're correct
# regardless of which copy of the package (if more than one ever exists
# again) actually got imported.
ENV POLYPRINTER_HOME=/app

# No default CMD/ENTRYPOINT — docker-compose.yml sets the command per
# service (scout vs dashboard) from this one image.
