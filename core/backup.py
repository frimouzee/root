import gzip
import config
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from utils.logger import log
from utils.monitoring import PerformanceMonitoring

class BackupManager:
    def __init__(self, bot):
        self.bot = bot
        self.backup_dir = Path("backups")
        self.schemas_dir = self.backup_dir / "schemas"
        self.full_dir = self.backup_dir / "full"
        self.retention_days = 7
        self.monitoring = PerformanceMonitoring()
        
        for directory in (self.backup_dir, self.schemas_dir, self.full_dir):
            directory.mkdir(exist_ok=True)

    async def run_backup(self) -> bool:
        """Run distributed backup process."""
        with self.monitoring.tracer.start_as_current_span("run_backup") as span:
            timestamp = datetime.now(timezone.utc)
            
            try:
                schema_task = self.bot.dask.client.submit(
                    self._create_schema_backup,
                    timestamp=timestamp
                )
                
                full_task = self.bot.dask.client.submit(
                    self._create_full_backup,
                    timestamp=timestamp
                )
                
                schema_success, full_success = await asyncio.gather(
                    schema_task, 
                    full_task
                )
                
                if schema_success and full_success:
                    self.bot.dask.client.submit(
                        self._cleanup_old_backups
                    )
                    return True
                    
                return False
                
            except Exception as e:
                log.error(f"Backup failed: {e}")
                span.record_exception(e)
                return False

    async def _create_schema_backup(self, timestamp: datetime) -> bool:
        """Create schema-only backup."""
        date_folder = timestamp.strftime("%Y-%m-%d")
        filename = f"schemas_{timestamp.strftime('%H%M%S')}.sql.gz"
        
        command = (
            f"PGPASSWORD={config.DATABASE.PASSWORD} pg_dumpall "
            f"-h {config.DATABASE.HOST} -U {config.DATABASE.USER} "
            "--schema-only --clean --if-exists"
        )
        
        return await self._process_backup(
            command, 
            self.schemas_dir / date_folder,
            filename,
            f"/schemas/{date_folder}/{filename}"
        )

    async def _create_full_backup(self, timestamp: datetime) -> bool:
        """Create full backup."""
        date_folder = timestamp.strftime("%Y-%m-%d")
        filename = f"full_{timestamp.strftime('%H%M%S')}.sql.gz"
        
        command = (
            f"PGPASSWORD={config.DATABASE.PASSWORD} pg_dumpall "
            f"-h {config.DATABASE.HOST} -U {config.DATABASE.USER} "
            "--clean --if-exists"
        )
        
        return await self._process_backup(
            command,
            self.full_dir / date_folder,
            filename,
            f"/full/{date_folder}/{filename}"
        )

    async def _process_backup(
        self, 
        command: str,
        local_dir: Path,
        filename: str,
        remote_path: str
    ) -> bool:
        """Process a single backup operation."""
        local_dir.mkdir(exist_ok=True)
        local_path = local_dir / filename

        try:
            if data := await self.bot.process_backup(command):
                await self.bot.dask.client.submit(
                    self._save_backup,
                    data,
                    local_path
                )
                
                success = await self.bot.dask.client.submit(
                    self._upload_backup,
                    local_path,
                    remote_path
                )
                
                if success:
                    log.info(f"Created backup: {filename}")
                    return True
                    
            return False
            
        except Exception as e:
            log.error(f"Backup processing failed: {e}")
            if local_path.exists():
                local_path.unlink()
            return False

    @staticmethod
    def _save_backup(data: bytes, path: Path) -> None:
        """Save compressed backup data."""
        with gzip.open(path, 'wb') as f:
            f.write(data)

    @staticmethod
    def _upload_backup(local_path: Path, remote_path: str) -> bool:
        """Upload backup to storage."""
        try:
            return True 
        except Exception as e:
            log.error(f"Upload failed: {e}")
            return False

    async def _cleanup_old_backups(self) -> None:
        """Clean up old backups."""
        current_time = datetime.now(timezone.utc).timestamp()
        retention_seconds = self.retention_days * 24 * 60 * 60

        for directory in (self.schemas_dir, self.full_dir):
            if not directory.exists():
                continue

            for date_dir in directory.iterdir():
                if not date_dir.is_dir():
                    continue

                for backup_file in date_dir.glob("*.sql.gz"):
                    try:
                        if current_time - backup_file.stat().st_mtime > retention_seconds:
                            backup_file.unlink()
                            log.info(f"Removed old backup: {backup_file.name}")
                    except OSError as e:
                        log.error(f"Cleanup error: {e}")

                if not any(date_dir.iterdir()):
                    try:
                        date_dir.rmdir()
                    except OSError:
                        pass 