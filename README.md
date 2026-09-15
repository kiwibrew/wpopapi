# WorldPop Population API

This FastAPI service queries current-year WorldPop population rasters by country. It has point, radius, and GeoJSON queries. It also includes authenticated Swagger and pages to administer users and tiles.

## How it Works

The service gets population data from country-specific WorldPop GeoTIFF raster tiles. The `iso3` parameter passed to the service selects one tile. The code must contain three alphabetic characters. The service removes surrounding spaces from the code and converts it to uppercase. 

The service gets a tile from its cache or downloads it from WorldPop with HTTPX. The current configuration uses constrained, 100-meter tiles for 2025. Rasterio opens the GeoTIFF and reads its geographic coordinates and population-cell values. The service moves each blocking raster read and file write to a worker thread.

All geographic input uses longitude and latitude in WGS 84 coordinates. Each route returns an integer in `{"pop": 12345}`. The service rounds a selected cell value or a calculated sum to the nearest integer.

`GET /api/pop` first checks that the point is inside the raster bounds. If the point is outside, the API returns HTTP 422 and the normalized country code. If the point is inside, Rasterio then samples the raster cell at `lon`, `lat` and returns the population value of the cell. A masked NoData sample returns `0`.

`GET /api/pop-radius` first checks that the center point is inside the raster bounds. It also checks the configured radius range. PyProj makes an azimuthal-equidistant projection centered on the point. Shapely makes a meter-based circular buffer in that projection, then converts it to WGS 84.

`POST /api/pop-shape` accepts a multipart `geojson_file` and `iso3`. FastAPI limits the uploaded file size before it parses the JSON. Shapely converts GeoJSON geometries, Features, and FeatureCollections to geometries. The route checks the configured vertex limit and requires at least one geometry to intersect the raster bounds.

For radius and shape queries, Rasterio reads only the raster window that overlaps the geometry bounds. Shapely finds the area where each query geometry overlaps an unmasked cell. The service treats each cell population as evenly distributed. It multiplies the cell value by its covered fraction before it calculates the sum.

Before a shape calculation, Shapely merges overlapping geometries. This prevents duplicate cell counts. Point and line GeoJSON geometries have zero area. They add no population unless an area geometry also covers cells.

The service checks against the rectangular bounds of the selected raster tile. It does not use a separate country-boundary polygon. A radius or GeoJSON geometry can extend outside the country when it overlaps the tile. The calculation includes only unmasked cells in the intersecting tile window.

| Library | Use |
| --- | --- |
| HTTPX | Download a country-specific raster tile from WorldPop. |
| Rasterio | Open GeoTIFF tiles, sample point cells, read raster windows, and make geometry masks. |
| Shapely | Convert GeoJSON to geometries, test intersections, and make the radius buffer. |
| PyProj | Convert the meter-based radius buffer between its local projection and WGS 84. |
| NumPy | Select unmasked cells and calculate the population sum. |

## Run the service

Docker Compose is the only supported application environment. CPython and all application tools run in the `app` container.

1. Copy `.env.example` to `.env` and set a long, random `SECRET_KEY`.
2. Set `SESSION_COOKIE_SECURE=true` for production HTTPS.
3. Copy `docker-compose.yml.example` to `docker-compose.yml`.
4. Build and start the service:

```bash
docker compose up --build
```

The application is available at `http://localhost:8002`. The SQLite database and downloaded rasters remain in the bind-mounted `app/data` directory.

Do not install Python packages or run Python tools on the host. Declare dependencies in `requirements.txt`, then rebuild the image.

## Configuration

Copy `.env.example` to `.env`. Change the values there, then restart the app container. `app/config.py` defines the settings, their types, and their defaults. Environment variables override those defaults.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `APP_NAME` | `WorldPop Population API` | Name shown in the UI and OpenAPI metadata |
| `DATABASE_URL` | Required | SQLAlchemy database connection URL |
| `TILE_CACHE_DIR` | `/app/data/tiles` | Directory for downloaded WorldPop rasters |
| `TILE_CACHE_EXPIRY_DAYS` | `365` | Number of days before an unused cached tile expires |
| `TILE_DOWNLOAD_TIMEOUT_SECONDS` | `120` | Timeout for a WorldPop raster download |
| `POP_RADIUS_MIN_METERS` | `1` | Smallest accepted radius query |
| `POP_RADIUS_MAX_METERS` | `100000` | Largest accepted radius query |
| `GEOJSON_MAX_SIZE_BYTES` | `5242880` (5 MiB) | Largest accepted GeoJSON upload |
| `GEOJSON_MAX_VERTICES` | `10000` | Most vertices accepted in one GeoJSON upload |
| `PASSWORD_MIN_LENGTH` | `12` | Minimum account password length |
| `PASSWORD_MAX_LENGTH` | `128` | Maximum account password length |
| `SECRET_KEY` | Required | Secret used to sign JWTs and CSRF tokens. Use a long random value. |
| `ALGORITHM` | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Lifetime of a short-lived API JWT |
| `SESSION_EXPIRE_MINUTES` | `10080` (7 days) | Lifetime of a browser session |
| `SESSION_COOKIE_SECURE` | `false` | Send session cookies only over HTTPS when `true` |
| `SMTP_ENABLED` | `false` | Enable password-reset email delivery |
| `SMTP_HOST` | `localhost` | SMTP server host |
| `SMTP_PORT` | `25` | SMTP server port |
| `SMTP_SENDER` | `noreply@example.invalid` | Password-reset sender address |

For example, to accept GeoJSON with up to 50,000 vertices, set `GEOJSON_MAX_VERTICES=50000` in `.env`, then restart the service:

```bash
docker compose up -d --force-recreate app
```

The WorldPop dataset, release, version, and year are developer-level constants at the end of `app/config.py`. Change them there and rebuild the image to change the raster release.

## Database and first administrator

The container applies Alembic migrations when it starts. Use these commands for explicit migration work:

```bash
docker compose run --rm app alembic upgrade head
docker compose run --rm app alembic revision --autogenerate -m "description"
```

Create or remove an account with the shared user service:

```bash
docker compose run --rm app python manage_users.py create admin@example.com 'a-long-password'
docker compose run --rm app python manage_users.py remove user@example.com
```

The command does not print a password, password hash, JWT, or persistent token.

## Authentication and UI

Open `/` and sign in with an administrator account. Authenticated users can access these pages:

- `/docs` for Swagger
- `/app-docs` for this guide
- `/manage-users` for their visible account information

Administrators manage all users. API users get a random persistent bearer token. Administrators never get one. `/token` exchanges a valid email and password for a short-lived JWT. Browser sessions use a separate typed JWT in an HTTP-only cookie.

Use `/forgot-password` to request a reset code. When SMTP delivery is enabled, the service emails a one-time code. The code expires after 30 minutes. Paste it into `/reset-password`. The request acknowledgement is the same whether or not an eligible account exists.

Send API credentials in `Authorization: Bearer TOKEN`. The application tries a persistent token first. It then accepts only an access-type JWT. Inactive accounts receive HTTP 403.

## Checks

Run every check through Compose:

```bash
docker compose run --rm app pytest
docker compose run --rm app pytest --cov=app
docker compose run --rm app ruff check .
docker compose run --rm app ruff format --check .
docker compose run --rm app mypy app/
```

Tests use a separate SQLite database. Rollback transactions isolate database changes.

## Structure

```text
app/
├── main.py
├── config.py
├── database.py
├── dependencies.py
├── models/
├── schemas/
├── routers/
├── services/
├── repositories/
└── templates/
tests/
├── conftest.py
├── test_routers/
└── test_services/
migrations/
```
