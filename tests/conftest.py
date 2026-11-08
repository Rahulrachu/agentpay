"""Keep test runs hermetic: importing app.main builds a module-level app, so point it at an
in-memory database and mock mode before any test module imports it."""
import os

os.environ["DB_PATH"] = ":memory:"
os.environ["MOCK_MODE"] = "1"
