"""
conftest.py — root-level pytest fixtures.

MUST set RAZORPAY_KEY_ID before any import of server.config, because the
boot guard fires at module-level (settings = Settings()). Setting via
os.environ before the first import satisfies the guard with a test key.
"""

import os

# Set test environment variables BEFORE importing anything from server.*
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_placeholder_ci")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret_placeholder")
os.environ.setdefault("GROQ_API_KEY", "gsk-test-placeholder")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STUB_MODE", "true")
os.environ.setdefault("BUYER_AGENT_PRIVATE_KEY_PATH", "./keys/buyer_es256.pem")
os.environ.setdefault("BUYER_AGENT_PUBLIC_KEY_PATH", "./keys/buyer_es256_pub.pem")
os.environ.setdefault("MERCHANT_AGENT_PRIVATE_KEY_PATH", "./keys/merchant_es256.pem")
os.environ.setdefault("MERCHANT_AGENT_PUBLIC_KEY_PATH", "./keys/merchant_es256_pub.pem")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from server.db.models import Base
from server.mandate.issuer import buyer_keys, ensure_keypairs


@pytest.fixture(scope="session", autouse=True)
def _ensure_keys():
    """Generate test keypairs once per test session."""
    ensure_keypairs()


@pytest.fixture
def test_db() -> Session:
    """
    In-memory SQLite database, fresh for each test function.
    Tables are created and dropped around each test.
    """
    # StaticPool, so every thread shares the one connection.
    #
    # An in-memory SQLite database lives inside its connection: the default pool
    # hands a second thread a *new* connection, which is a new empty database
    # with no tables in it. TestClient runs the app on its own thread, so
    # without this a request would quietly operate on a different database than
    # the test inspects.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    db = Factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(test_db):
    """
    FastAPI test client bound to the per-test in-memory database.

    The dependency override is what makes assertions possible: without it the
    app opens its own session against the configured DATABASE_URL and the test
    inspects a database the request never touched.
    """
    from fastapi.testclient import TestClient

    from server.db.session import get_db
    from server.main import app

    app.dependency_overrides[get_db] = lambda: test_db
    try:
        # Deliberately not entered as a context manager: that would run the
        # lifespan, whose boot sweep and reconciler open their own sessions
        # against the configured DATABASE_URL — a different in-memory database
        # with no tables in it. The routes under test take their session from
        # the override above.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
