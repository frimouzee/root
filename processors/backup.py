import subprocess
import logging
from pathlib import Path
from typing import Optional
import ftplib
from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

def run_pg_dump(command: str) -> Optional[bytes]:
    """Execute pg_dump command in a separate process."""
    with tracer.start_as_current_span("run_pg_dump") as span:
        span.set_attribute("command", command)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                bufsize=8192  
            )
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                error = stderr.decode()
                log.error(f"Backup failed: {error}")
                span.record_exception(Exception(error))
                return None
                
            return stdout
        except Exception as e:
            log.error(f"Backup error: {e}")
            span.record_exception(e)
            return None

def process_bunny_upload(
    local_path: Path,
    remote_path: str,
    bunny_host: str,
    bunny_user: str,
    bunny_pass: str,
    chunk_size: int = 8192
) -> bool:
    """Process Bunny Storage upload with chunked transfer."""
    with tracer.start_as_current_span("bunny_upload") as span:
        span.set_attributes({
            "local_path": str(local_path),
            "remote_path": remote_path,
            "file_size": local_path.stat().st_size
        })
        
        try:
            with ftplib.FTP(bunny_host, timeout=30) as ftp:
                ftp.login(bunny_user, bunny_pass)
                
                current_path = ""
                for part in remote_path.split("/")[:-1]:
                    current_path += f"/{part}"
                    try:
                        ftp.mkd(current_path)
                    except ftplib.error_perm:
                        pass

                with open(local_path, "rb") as file:
                    ftp.storbinary(
                        f"STOR {remote_path}",
                        file,
                        blocksize=chunk_size
                    )
                
                log.info(f"Uploaded {local_path} to Bunny Storage")
                return True
                
        except Exception as e:
            log.error(f"Bunny Storage upload failed: {e}")
            span.record_exception(e)
            return False 