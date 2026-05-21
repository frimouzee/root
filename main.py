import asyncio
import config
from core.bot import Evict
from utils.logger import setup_logging, log
from utils.monitoring import setup_monitoring
from core.dask import DaskManager
import math
import signal
import sys

CLUSTER_COUNT = 2
SHARD_COUNT = 6

async def start_cluster(cluster_id: int, shard_ids: list, total_shards: int, dask_client) -> tuple:
    """Start a single cluster."""
    try:
        log.info(f"Starting cluster {cluster_id} with shards {shard_ids}")
        
        log.info("Creating bot instance...")
        bot = Evict(
            cluster_id=cluster_id,
            cluster_count=CLUSTER_COUNT,
            dask_client=dask_client,
            shard_ids=shard_ids,
            shard_count=total_shards,
            owner_ids=config.CLIENT.OWNER_IDS,
            description=config.CLIENT.DESCRIPTION,
        )
        
        log.info(f"Bot instance created for cluster {cluster_id}")
        log.info("Starting bot login process...")
        
        bot_task = asyncio.create_task(bot.start(config.CLIENT.TOKEN))
        
        try:
            await asyncio.wait_for(bot._is_ready.wait(), timeout=120)
            log.info(f"Cluster {cluster_id} is ready")
            return bot, bot_task
        except asyncio.TimeoutError:
            log.warning(f"Timeout waiting for cluster {cluster_id} ready event, but bot may still be operational")
            return bot, bot_task
            
    except Exception as e:
        log.error(f"Failed to start cluster {cluster_id}", exc_info=True)
        raise

async def main():
    """Main entry point."""
    try:
        setup_logging()
        log.info("Logging setup complete")
        
        setup_monitoring()
        log.info("Monitoring setup complete")
        
        log.info("Initializing Dask...")
        dask_manager = DaskManager()
        await dask_manager.setup()
        dask_client = dask_manager.client
        log.info("Dask initialization complete")
        
        log.info(f"Config TOTAL_SHARDS: {SHARD_COUNT}")
        log.info(f"Config CLUSTER_COUNT: {CLUSTER_COUNT}")
        
        total_shards = SHARD_COUNT
        cluster_count = CLUSTER_COUNT
        shards_per_cluster = math.ceil(total_shards / cluster_count)
        
        log.info(f"Starting {cluster_count} clusters with {total_shards} total shards...")
        
        cluster_tasks = []
        for cluster_id in range(cluster_count):
            start_shard = cluster_id * shards_per_cluster
            end_shard = min((cluster_id + 1) * shards_per_cluster, total_shards)
            shard_ids = list(range(start_shard, end_shard))
            
            log.info(f"Preparing cluster {cluster_id} with shards {shard_ids}")
            
            task = asyncio.create_task(
                start_cluster(cluster_id, shard_ids, total_shards, dask_client)
            )
            cluster_tasks.append(task)
        
        try:
            clusters = []
            results = await asyncio.gather(*cluster_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.error(f"Cluster failed to start: {result}")
                else:
                    bot, bot_task = result
                    clusters.append((bot, bot_task))
            
            if not clusters:
                raise RuntimeError("No clusters started successfully")
            
            log.info(f"Successfully started {len(clusters)} clusters")
                
            while True:
                await asyncio.sleep(1)
                
                for bot, task in clusters:
                    if task.done() and task.exception():
                        log.error(f"Bot task failed: {task.exception()}")
                
        except Exception as e:
            log.error(f"Error while running clusters: {e}", exc_info=True)
            raise
        finally:
            for bot, _ in clusters:
                try:
                    await bot.close()
                except:
                    pass
            
            if 'dask_manager' in locals():
                await dask_manager.cleanup()
                
    except Exception as e:
        log.error(f"Fatal error in main: {e}", exc_info=True)
        raise

def handle_signal(signum, frame):
    """Handle termination signals."""
    log.info(f"Received signal {signum}")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
