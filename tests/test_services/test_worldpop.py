import warnings
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

import app.services.worldpop as worldpop
from app.routers import auth
from app.config import dataset, release, version, year
from app.services.worldpop import (
    CoordinatesOutsideCountryError,
    GeoJSONOutsideCountryError,
    WorldPopService,
    TileNotFoundError,
    count_geojson_vertices,
    get_worldpop_url,
    normalize_iso3,
    parse_iso3_csv,
)


def _write_test_raster(path):
    data = np.arange(1, 101, dtype=np.int16).reshape((10, 10))
    transform = from_origin(0, 10, 1, 1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def test_url_generation():
    assert get_worldpop_url("nzl") == (
        "https://data.worldpop.org/GIS/Population/"
        f"{dataset}/{release}/{year}/NZL/{version}/100m/constrained/"
        f"nzl_pop_{year}_CN_100m_{release}_{version}.tif"
    )


def test_normalize_iso3():
    assert normalize_iso3("nzl") == "NZL"


def test_parse_iso3_csv_normalizes_and_deduplicates():
    assert parse_iso3_csv("aus, nzl, AUS,usa") == ["AUS", "NZL", "USA"]


def test_parse_iso3_csv_rejects_empty_input():
    with pytest.raises(
        ValueError,
        match="iso3 list must contain at least one valid three-letter ISO country code",
    ):
        parse_iso3_csv(" , ")


def test_vertex_count():
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 0], [1, 1], [2, 2]],
                },
            },
        ],
    }

    assert count_geojson_vertices(geojson) == 7


@pytest.mark.asyncio
async def test_pop_queries_with_local_raster(
    tmp_path, monkeypatch, suppress_rasterio_affine_warning
):
    raster_path = tmp_path / "NZL_pop.tif"
    _write_test_raster(raster_path)

    service = WorldPopService(db=object())
    tile_requests = []

    async def fake_get_tile_path(iso3):
        tile_requests.append(iso3)
        return str(raster_path)

    service.get_tile_path = fake_get_tile_path  # type: ignore[method-assign]
    monkeypatch.setattr(
        worldpop,
        "settings",
        SimpleNamespace(
            POP_RADIUS_MIN_METERS=1,
            POP_RADIUS_MAX_METERS=2_000_000,
            GEOJSON_MAX_VERTICES=10_000,
        ),
    )

    assert await service.get_pop("nzl", 7.5, 2.5) == 23
    assert await service.get_pop_radius("nzl", 7.5, 2.5, 2_000_000) == 5050
    with pytest.raises(
        CoordinatesOutsideCountryError,
        match=(
            "the submitted point is not within the bounds of the WorldPop tile "
            "for country code NZL"
        ),
    ):
        await service.get_pop_radius("nzl", -80.0, 170.0, 10_000)

    with pytest.raises(
        CoordinatesOutsideCountryError,
        match=(
            "the submitted point is not within the bounds of the WorldPop tile "
            "for country code NZL"
        ),
    ):
        await service.get_pop("nzl", -80.0, 170.0)

    geojson = {
        "type": "Polygon",
        "coordinates": [[[0, 10], [2, 10], [2, 8], [0, 8], [0, 10]]],
    }
    assert await service.get_pop_shape("nzl", geojson) == 26

    partial_geojson = {
        "type": "Polygon",
        "coordinates": [[[9, 0], [9.5, 0], [9.5, 0.5], [9, 0.5], [9, 0]]],
    }
    assert await service.get_pop_shape("nzl", partial_geojson) == 25

    outside_geojson = {
        "type": "Polygon",
        "coordinates": [[[20, 20], [21, 20], [21, 21], [20, 21], [20, 20]]],
    }
    with pytest.raises(
        GeoJSONOutsideCountryError,
        match="geojson is not inside the bounds of country NZL",
    ):
        await service.get_pop_shape("nzl", outside_geojson)

    assert tile_requests == ["NZL", "NZL", "NZL", "NZL", "NZL", "NZL", "NZL"]


def test_population_sum_weights_partially_covered_cells(
    tmp_path, suppress_rasterio_affine_warning
):
    raster_path = tmp_path / "NZL_pop.tif"
    _write_test_raster(raster_path)

    population = worldpop._sum_raster_population_with_cell_coverage(
        str(raster_path),
        [box(9, 0, 9.5, 0.5)],
    )

    assert population == 25
    assert worldpop._sum_raster_population_with_cell_coverage(
        str(raster_path),
        [box(9, 0, 9.75, 1), box(9.25, 0, 10, 1)],
    ) == 100


@pytest.fixture
def suppress_rasterio_affine_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PendingDeprecationWarning)
        yield


@pytest.mark.asyncio
async def test_fill_tile_cache_uses_each_iso3_once(monkeypatch):
    service = WorldPopService(db=object())
    seen = []

    async def fake_get_tile_path(iso3, skip_missing=False):
        seen.append((iso3, skip_missing))
        return f"/tmp/{iso3}.tif"

    service.get_tile_path = fake_get_tile_path  # type: ignore[method-assign]

    cached_paths = await service.fill_tile_cache("aus, NZL,aus,usa")

    assert seen == [("AUS", True), ("NZL", True), ("USA", True)]
    assert cached_paths == ["/tmp/AUS.tif", "/tmp/NZL.tif", "/tmp/USA.tif"]


@pytest.mark.asyncio
async def test_fill_tile_cache_skips_missing_tiles(monkeypatch):
    service = WorldPopService(db=object())
    seen = []

    async def fake_get_tile_path(iso3, skip_missing=False):
        seen.append((iso3, skip_missing))
        if iso3 == "NZL":
            raise TileNotFoundError(iso3)
        return f"/tmp/{iso3}.tif"

    service.get_tile_path = fake_get_tile_path  # type: ignore[method-assign]

    cached_paths = await service.fill_tile_cache("aus,nzl,usa")

    assert seen == [("AUS", True), ("NZL", True), ("USA", True)]
    assert cached_paths == ["/tmp/AUS.tif", "/tmp/USA.tif"]


@pytest.mark.asyncio
async def test_get_tile_path_propagates_missing_tile_error(monkeypatch):
    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return None

    class _FakeDB:
        async def execute(self, *args, **kwargs):
            return _FakeScalarResult()

    service = WorldPopService(db=_FakeDB())

    async def fake_download(*args, **kwargs):
        raise TileNotFoundError("NZL")

    monkeypatch.setattr(service, "download_tile_file", fake_download)

    with pytest.raises(TileNotFoundError, match="no tile available for country NZL"):
        await service.get_tile_path("nzl")


@pytest.mark.asyncio
async def test_get_tile_path_does_not_write_when_tile_is_cached():
    class FakeRepository:
        async def get_by_tile_id(self, _tile_id):
            return SimpleNamespace(file_path="/tmp/NZL.tif")

        async def flush(self):
            pytest.fail("cached tile lookup must not flush a database write")

    service = WorldPopService(db=object())
    service.repository = FakeRepository()

    assert await service.get_tile_path("nzl") == "/tmp/NZL.tif"


@pytest.mark.asyncio
async def test_cache_fill_releases_database_session_before_downloading(monkeypatch):
    events = []

    class FakeTransaction:
        async def __aenter__(self):
            events.append("persist transaction started")

        async def __aexit__(self, *_args):
            events.append("persist transaction ended")

    class FakeSession:
        async def __aenter__(self):
            events.append("session opened")
            return self

        async def __aexit__(self, *_args):
            events.append("session closed")

        def begin(self):
            return FakeTransaction()

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    async def fake_get_cached_tile_path(_self, _iso3):
        events.append("cache checked")
        return None

    async def fake_download_tile_file(_iso3, skip_missing=False):
        assert skip_missing is True
        assert events == ["session opened", "cache checked", "session closed"]
        events.append("tile downloaded")
        return "/tmp/NZL.tif"

    async def fake_cache_downloaded_tile(_self, _iso3, _file_path):
        events.append("tile cached")
        return _file_path

    monkeypatch.setattr(auth, "AsyncSessionLocal", FakeSessionFactory())
    monkeypatch.setattr(
        auth.WorldPopService, "get_cached_tile_path", fake_get_cached_tile_path
    )
    monkeypatch.setattr(
        auth.WorldPopService, "download_tile_file", fake_download_tile_file
    )
    monkeypatch.setattr(
        auth.WorldPopService, "cache_downloaded_tile", fake_cache_downloaded_tile
    )

    await auth._process_tile_cache_fill("nzl")

    assert events == [
        "session opened",
        "cache checked",
        "session closed",
        "tile downloaded",
        "session opened",
        "persist transaction started",
        "tile cached",
        "persist transaction ended",
        "session closed",
    ]
