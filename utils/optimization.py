import os
import psutil
import onnxruntime
from typing import Dict

def get_cpu_settings() -> Dict[str, str]:
    """Get optimal CPU environment settings"""
    logical_cpu_count = psutil.cpu_count(logical=False)
    return {
        "OMP_NUM_THREADS": str(logical_cpu_count),
        "ONNXRUNTIME_THREAD_COUNT": str(logical_cpu_count),
        "OMP_WAIT_POLICY": "PASSIVE",
        "OMP_PROC_BIND": "CLOSE",
        "OMP_PLACES": "cores",
        "KMP_AFFINITY": "granularity=fine,compact,1,0",
        "OPENBLAS_NUM_THREADS": str(logical_cpu_count),
        "MKL_NUM_THREADS": str(logical_cpu_count),
        "VECLIB_MAXIMUM_THREADS": str(logical_cpu_count),
        "NUMEXPR_NUM_THREADS": str(logical_cpu_count),
        "ONNXRUNTIME_DISABLE_THREAD_AFFINITY": "1",
        "OMP_SCHEDULE": "static",
        "KMP_BLOCKTIME": "0",
        "KMP_SETTINGS": "0"
    }

def setup_onnx() -> onnxruntime.SessionOptions:
    """Configure ONNX runtime settings"""
    logical_cpu_count = psutil.cpu_count(logical=False)
    onnxruntime.set_default_logger_severity(3)
    
    sess_options = onnxruntime.SessionOptions()
    sess_options.intra_op_num_threads = logical_cpu_count
    sess_options.inter_op_num_threads = logical_cpu_count
    sess_options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    sess_options.enable_cpu_mem_arena = False
    sess_options.enable_mem_pattern = False
    sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_DISABLE_ALL
    
    return sess_options

def setup_cpu_optimizations():
    """Apply all CPU and ONNX optimizations"""
    for key, value in get_cpu_settings().items():
        os.environ[key] = value
    
    return setup_onnx() 