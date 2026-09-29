# Release checklist

Cut Agent supports Linux and Windows with Python 3.10–3.14 and FFmpeg/FFprobe 5 or newer. CI validates isolated wheel installation on both operating systems; browser regression runs on Linux Chromium.

1. Update `cut_agent.__version__` and `web/package.json` together, then run `uv lock`.
2. Run `uv run python tools/build_web_assets.py` and commit the refreshed `src/cut_agent/web_dist` files.
3. Run the fast, media, web, browser, and distribution checks documented in the README.
4. Add user-visible changes to the GitHub release notes and verify upgrade/recovery behavior when persisted artifacts changed.
5. Build with `uv build`, inspect with `uv run python tools/check_release.py`, and install the wheel in a clean environment with `uv run python tools/smoke_distribution.py dist/CUT_AGENT_WHEEL.whl`.
6. Tag the same version only after Linux and Windows CI pass. Never publish a package assembled from a dirty worktree.
