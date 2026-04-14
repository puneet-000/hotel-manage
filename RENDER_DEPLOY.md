# Deploy to Render (Flask + Gunicorn)

## Files already added
- `render.yaml` for the Render Blueprint
- `requirements.txt` for Python dependencies
- `wsgi.py` for the Gunicorn entrypoint

## Steps
1. Push this project to a GitHub repository.
2. In Render, click `New` -> `Blueprint`.
3. Select your GitHub repository.
4. Render will detect `render.yaml` and create the web service automatically.

## Important notes
- The app uses SQLite and defaults to `database.db`.
- On Render free instances, local filesystem data can be lost after redeploys or restarts.
- If you want the database to persist, create a Persistent Disk and set `DB_PATH=/var/data/database.db`.
- Mount that disk at `/var/data`.

## Recommended environment variables
- `SECRET_KEY`
- `OWNER_LOGIN_ID`
- `OWNER_LOGIN_PASSWORD`
