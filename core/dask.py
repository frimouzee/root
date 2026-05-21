from distributed import Client
from typing import Optional
from utils.logger import log
import config
import dask
from dask.distributed import LocalCluster
import socket

class DaskManager:
    def __init__(self):
        self.client = None
        self.cluster = None

    async def setup(self):
        """Initialize the Dask cluster and client."""
        try:
            dask.config.set({
                'distributed.dashboard.link': 'http://{host}:{port}/status',
                'distributed.dashboard.address': ':8787',  
            })

            self.cluster = await LocalCluster(
                n_workers=4,
                threads_per_worker=4,
                memory_limit='1GB',
                dashboard_address=':8787',  
                host='0.0.0.0', 
                asynchronous=True,
            )

            self.client = await Client(self.cluster, asynchronous=True)
            log.info(f"Dask dashboard available at: http://{socket.gethostbyname(socket.gethostname())}:8787/status")
            return self.client

        except Exception as e:
            log.error(f"Failed to initialize Dask: {e}")
            raise

    async def cleanup(self):
        """Cleanup Dask resources."""
        if self.client:
            await self.client.close()
        if self.cluster:
            await self.cluster.close() 