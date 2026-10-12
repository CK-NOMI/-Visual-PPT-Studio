"""Local-only server entrypoint."""
import os
import uvicorn
from studio.server import load_local_env
if __name__=='__main__':
    load_local_env()
    uvicorn.run('studio.server:app',host=os.getenv('PPT_STUDIO_HOST','127.0.0.1'),
                port=int(os.getenv('PPT_STUDIO_PORT','8765')),reload=False)
