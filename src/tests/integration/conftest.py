import pytest
from uuid import uuid4

from elasticsearch import AsyncElasticsearch
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from pymongo import AsyncMongoClient
from testcontainers.elasticsearch import ElasticSearchContainer
from testcontainers.mongodb import MongoDbContainer

from asgi_lifespan import LifespanManager

from src.config import Settings
from src.core.database import DatabaseManager
from src.main import create_app


@pytest.fixture(scope="session")
def mongodb_container():
    with MongoDbContainer("mongo:8") as c:
        yield c


@pytest.fixture(scope="session")
def mongodb_url(mongodb_container):
    return mongodb_container.get_connection_url()


@pytest.fixture(scope="session")
def es_container():
    with ElasticSearchContainer("elasticsearch:9.1.0") as c:
        yield c


@pytest.fixture(scope="session")
def es_url(es_container):
    host = es_container.get_container_host_ip()
    port = es_container.get_exposed_port(9200)
    return f"http://{host}:{port}"


@pytest.fixture
async def app(mongodb_url, es_url):
    suffix = uuid4().hex[:8]
    settings = Settings(
        mongodb_url=mongodb_url,
        mongodb_database=f"test_{suffix}",
        elasticsearch_url=es_url,
        elasticsearch_index=f"events_{suffix}",
        redis_url="redis://fake",
        batch_timeout=0.5,  # fast flush for tests
    )

    db = DatabaseManager(settings)
    db.mongo_client = AsyncMongoClient(mongodb_url)
    db.mongo_db = db.mongo_client[settings.mongodb_database]
    db.es_client = AsyncElasticsearch(es_url)
    db.redis_client = FakeAsyncRedis(decode_responses=True)

    application = create_app(settings=settings, db_manager=db)

    async with LifespanManager(application):
        yield application

        # Cleanup test data before lifespan shutdown closes clients
        await db.mongo_client.drop_database(settings.mongodb_database)
        await db.es_client.options(ignore_status=[404]).indices.delete(
            index=settings.elasticsearch_index
        )


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def wait_for_processing(app):
    """Wait for all queued events to be processed by the worker."""
    await app.state.queue.join()
    # Refresh ES index to make documents searchable
    await app.state.db.es_client.indices.refresh(
        index=app.state.settings.elasticsearch_index
    )
