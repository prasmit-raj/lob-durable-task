import os

import uvicorn

from lob.app import app

uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("LOB_PORT", "8000")),
            log_level="warning")
