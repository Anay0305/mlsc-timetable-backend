"""MongoDB persistence layer (Beanie + Motor).

`init_db()` is called from the FastAPI startup hook. All other modules access
collections through Beanie document classes defined in :mod:`server.db.models`.
"""

from server.db.init import close_db, init_db  # noqa: F401
