"""
Lazy connection to MongoDB.

The connection is established on first access, not at import time.
If MongoDB is unavailable, the app still starts — operations that
require MongoDB will fail individually rather than crashing the process.
"""
import logging
from decouple import config

logger = logging.getLogger(__name__)

# the collection strings for mongodb
ACTION_LOGS = 'action_logs'
NOTIFICATIONS = 'notifications'
COL_DPO_TOKENS = 'dpo_tokens'
COL_DPO_TOKEN_VERIFICATION = 'dpo_token_verification'

COL_PROFILE_UPDATE_APPROVALS = 'profile_update_approvals'
COL_NOTIFICATIONS = 'notifications'

# aggregator responses
COL_DPO_RESPONSES = 'dpo_responses'
COL_YO_RESPONSES = 'yo_responses'


class _LazyMongoClient:
    """Proxy that defers MongoClient creation until first attribute access."""

    def __init__(self):
        self._client = None
        self._db = None

    def _connect(self):
        if self._client is None:
            try:
                from pymongo import MongoClient
                host = config("MONGO_HOST", default="mongodb://localhost:27017")
                db_name = config("MONGO_DATABASE", default="dinify")
                self._client = MongoClient(
                    host,
                    uuidRepresentation='standard',
                    serverSelectionTimeoutMS=2000,
                )
                self._db = self._client[db_name]
            except Exception as exc:
                logger.error("MongoDB connection failed: %s", exc)
                self._client = None
                self._db = None

    @property
    def db(self):
        self._connect()
        return self._db


_lazy = _LazyMongoClient()


class _MongoDBProxy:
    """
    Dict-like proxy for MONGO_DB that lazily connects.
    Supports MONGO_DB[collection_name] access pattern used throughout the codebase.
    """

    def __getitem__(self, name):
        db = _lazy.db
        if db is None:
            raise RuntimeError(
                f"MongoDB is not available. Cannot access collection '{name}'."
            )
        return db[name]

    def __getattr__(self, name):
        db = _lazy.db
        if db is None:
            raise RuntimeError(
                f"MongoDB is not available. Cannot access attribute '{name}'."
            )
        return getattr(db, name)


MONGO_DB = _MongoDBProxy()
