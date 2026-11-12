"""The web dashboard that runs on the frame (`photopainter serve`, PRD.md tasks W1 and W2).

- `server.py`  Flask app factory, the JSON API and `main()` for the CLI
- `jobs.py`    ticks and preview renders, each in a child process
- `photos.py`  the photo folder: names, uploads, the trash, preview cache files
- `health.py`  live health: Wi-Fi, clock, temperature, power, data sources
- `static/`    the single page (plain HTML, CSS and JS; no build step, no CDN)
"""
